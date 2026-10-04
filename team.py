"""Small CLI coordinator: delegate before exploring, or finish a trivial task directly."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
import uuid

import worker

PLAN_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["summary", "tasks", "report"],
    "properties": {
        "summary": {"type": "string"},
        "report": {"anyOf": [worker.REPORT_SCHEMA, {"type": "null"}]},
        "tasks": {"type": "array", "maxItems": 3, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["id", "role", "scope", "prompt", "acceptance"],
            "properties": {
                "id": {"type": "string"},
                "role": {"type": "string", "enum": list(worker.ROLES)},
                "scope": {"type": "array", "items": {"type": "string"}},
                "prompt": {"type": "string"},
                "acceptance": {"type": "array", "items": {"type": "string"}},
            },
        }},
    },
}


def invoke(cli, cwd, output, prompt, schema, model, effort, timeout, env, session_id=None):
    output.mkdir(parents=True, exist_ok=False)
    worker.write_json(output / "schema.json", schema)
    # The first turn may finish a trivial task directly. Delegated work must be
    # assigned before exploration; this is a prompt policy, not a tool sandbox.
    task = dict(cwd=str(cwd), role="implement")
    command = worker.build_command(cli, task, model, effort, output, session_id)
    worker.write_json(output / "command.json", command)
    (output / "prompt.txt").write_text(prompt, encoding="utf-8")
    execution = worker.run_process(command, str(cwd), prompt, output, timeout, env=env)
    events = worker.parse_events(output / "events.jsonl")
    try:
        final = worker.read_json(output / "final.json")
    except (ValueError, OSError):
        final = None
    valid = (execution["exit_code"] == 0 and execution["termination"] == "exited"
             and events["completed_turns"] == 1 and not events["errors"]
             and isinstance(final, dict) and bool(events["session_id"])
             and (not session_id or session_id == events["session_id"]))
    return dict(valid=valid, execution=execution, events=events, final=final)


def run_team(prompt, cwd, output, cli=None, main_model="gpt-6-astra", worker_model=worker.DEFAULT_MODEL,
             effort="high", worker_effort="high", timeout=600, env=None):
    with worker.directory_lock(worker.git_root(cwd)):
        return _run_team(prompt, cwd, output, cli, main_model, worker_model, effort, worker_effort, timeout, env)


def _run_team(prompt, cwd, output, cli, main_model, worker_model, effort, worker_effort, timeout, env):
    cwd, output = Path(cwd).resolve(), Path(output).resolve()
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    worker.git_root(cwd)
    output.mkdir(parents=True, exist_ok=False)
    cli = cli or ["codex"]
    plan = invoke(cli, cwd, output / "plan", (
        "You are the main coordinator. Minimize your own token usage while preserving quality. "
        "Choose from the user request alone: delegate, or finish a truly trivial task directly. "
        "Default to ONE worker owning investigation, implementation and relevant tests end to end. "
        "When delegating, do not call tools, read files, search, or design the implementation first. "
        "Unknown file locations and implementation details are for the worker to discover. "
        "Give the goal, constraints and observable acceptance criteria, not a detailed solution. "
        "A host runner executes the tasks and resumes this session with short results for final review. "
        "Return report=null with 1-3 tasks. Split only clearly independent workstreams already apparent "
        "in the request; do not create separate research, implementation and review tasks by default. "
        "Roles are research, implement, review. Use relative file/directory "
        "scopes (not globs), including test files. Disjoint scopes run in parallel; overlapping scopes "
        "with an implement task run in list order. Use scope=['.'] for a single worker when paths are unknown. "
        "Make prompts self-contained, including relevant testing and fixing failures before returning. "
        "For a truly trivial known-location change needing no investigation, finish it NOW, run relevant "
        "checks, and return tasks=[] with a complete report. There is no second turn for this route. "
        "Do not run native subagents, launch AI CLIs, commit, or change Git configuration. "
        "Do not prescribe later tasks that depend on findings you have not received.\nUser task:\n" + prompt),
        PLAN_SCHEMA, main_model, effort, timeout, env)
    worker.write_json(output / "plan/execution.json", plan["execution"])
    raw = plan["final"].get("tasks") if plan["valid"] else None
    direct_report = plan["final"].get("report") if plan["valid"] else None
    if (not isinstance(raw, list) or len(raw) > 3 or not all(isinstance(t, dict) for t in raw)
            or (raw and direct_report is not None) or (raw == [] and not worker.validate_report(direct_report))):
        result = dict(status="failed", summary="Main planning failed; inspect plan artifacts.",
                      main_usage=plan["events"]["usage"], worker_usage=None, actual_delegation=False,
                      main_valid=False, artifacts=str(output))
        worker.write_json(output / "summary.json", result)
        return result
    if not raw:
        result = dict(status=direct_report["status"], summary=direct_report["summary"][:1200],
                      main_valid=True, main_session_id=plan["events"]["session_id"],
                      main_usage=plan["events"]["usage"], worker_usage=None, actual_delegation=False,
                      worker_status=None, main_report=direct_report, artifacts=str(output))
        worker.write_json(output / "summary.json", result)
        return result
    batch = None
    if raw:
        worker.write_json(output / "tasks.json", {"tasks": [dict(t, cwd=str(cwd)) for t in raw]})
        tasks = worker.load_tasks(output / "tasks.json")
        batch = worker.run_batch(tasks, output / "workers", cli=cli, model=worker_model,
                                 effort=worker_effort, timeout=timeout, env=env,
                                 _held_roots=(worker.git_root(cwd),))
    final = invoke(cli, cwd, output / "review", (
        "Continue as the main coordinator. Review the short worker results below and only the necessary "
        "changed code. Do not repeat broad exploration. Verify the user's acceptance criteria and relevant "
        "tests; repair small defects or report blocked if substantial work remains. "
        "Do not launch AI CLIs or native subagents. Do not commit. "
        "Return a concise final report using the supplied schema.\nWorker results:\n" +
        worker.main_report(batch)), worker.REPORT_SCHEMA, main_model, effort, timeout, env,
        session_id=plan["events"]["session_id"])
    worker.write_json(output / "review/execution.json", final["execution"])
    report = final["final"]
    valid = plan["valid"] and final["valid"] and worker.validate_report(report)
    result = dict(status=report["status"] if valid else "failed",
                  summary=report["summary"][:1200] if valid else "Main review failed; inspect artifacts.",
                  main_valid=valid, main_session_id=final["events"]["session_id"],
                  # The resumed session total already includes planning; do not add it twice.
                  main_usage=final["events"]["usage"] if valid else None,
                  worker_usage=batch["usage"] if batch else None,
                  actual_delegation=bool(batch), worker_status=batch["status"] if batch else None,
                  main_report=report, artifacts=str(output))
    worker.write_json(output / "summary.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt", type=Path, help="UTF-8 task file")
    parser.add_argument("--cwd", type=Path, default=Path.cwd())
    parser.add_argument("--main-model", default="gpt-6-astra")
    parser.add_argument("--worker-model", default=worker.DEFAULT_MODEL)
    parser.add_argument("--effort", default="high")
    parser.add_argument("--worker-effort", default="high")
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    output = args.output or Path("runs") / ("team-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
    try:
        result = run_team(args.prompt.read_text(encoding="utf-8-sig"), args.cwd, output, [args.codex],
                          args.main_model, args.worker_model, args.effort, args.worker_effort, args.timeout)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "completed" and result.get("worker_status") in (None, "completed") else 1
    except (ValueError, OSError) as error:
        print(f"team: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
