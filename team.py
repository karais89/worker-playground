"""CLI director: delegate, inspect read-only, and optionally continue workers once."""
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

REVIEW_SCHEMA = {
    **worker.REPORT_SCHEMA,
    "required": [*worker.REPORT_SCHEMA["required"], "followups"],
    "properties": {**worker.REPORT_SCHEMA["properties"], "followups": {
        "type": "array", "maxItems": 3, "items": {
            "type": "object", "additionalProperties": False, "required": ["id", "prompt"],
            "properties": {"id": {"type": "string"}, "prompt": {"type": "string"}},
        },
    }},
}


def invoke(cli, cwd, output, prompt, schema, model, effort, timeout, env, session_id=None):
    output.mkdir(parents=True, exist_ok=False)
    worker.write_json(output / "schema.json", schema)
    # The first turn may finish a trivial task directly. Delegated work must be
    # assigned before exploration; this is a prompt policy, not a tool sandbox.
    task = dict(cwd=str(cwd), role="review" if session_id else "implement")
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
    worker.write_json(output / "tasks.json", {"tasks": [dict(t, cwd=str(cwd)) for t in raw]})
    tasks = worker.load_tasks(output / "tasks.json")
    batch = worker.run_batch(tasks, output / "workers", cli=cli, model=worker_model,
                             effort=worker_effort, timeout=timeout, env=env,
                             _held_roots=(worker.git_root(cwd),))
    # One bounded feedback round; each selected worker keeps its original session,
    # model, permissions and scope. The coordinator never repairs code itself.
    usage_parts = [{"usage": batch["usage"]}]
    repair_count = 0
    for round_index in range(2):
        review_dir = output / ("review" if round_index == 0 else "review-final")
        final = invoke(cli, cwd, review_dir, (
            "Continue as the director in read-only mode. Read necessary changed files and tests to verify "
            "the user requirements. Do not repeat broad exploration, edit files, run tests/builds, launch "
            "AI CLIs or native subagents, or commit. Workers perform execution and corrections. "
            "Use observed_commands for evidence of command exit codes; worker summaries are claims, "
            "and an exit code alone does not prove correctness. Inspect relevant code. "
            "If corrections or additional checks are needed, return status=blocked and followups with "
            "existing worker IDs and concrete instructions within their original scope and role. "
            "Never assign edits to a research/review worker. Do not create new workers. "
            "Otherwise return followups=[] and the final report. " +
            ("There is ONE feedback round available. " if round_index == 0 else
             "No feedback rounds remain. Return followups=[]; report blocked if anything remains unresolved. ") +
            "\nWorker results:\n" + worker.main_report(batch)), REVIEW_SCHEMA,
            main_model, effort, timeout, env, session_id=plan["events"]["session_id"])
        worker.write_json(review_dir / "execution.json", final["execution"])
        review = final["final"]
        followups = review.get("followups") if final["valid"] and worker.validate_report(review) else None
        known = {t["id"]: t for t in batch["tasks"]}
        if (not isinstance(followups, list) or len(followups) > 3
                or any(not isinstance(f, dict) or not isinstance(f.get("id"), str) or f["id"] not in known
                       or not isinstance(f.get("prompt"), str) or not f["prompt"].strip() for f in followups)
                or len({f["id"] for f in followups}) != len(followups)
                or (followups and review["status"] != "blocked")):
            final["valid"] = False
            break
        if not followups:
            break
        if round_index or batch.get("scope_violations"):
            review["issues"].append("Feedback budget exhausted or scope violation prevents continuation.")
            break
        # Validate every continuation before spending tokens. Interrupted or
        # unmeasured sessions are not automatically retried.
        continuation_started = False
        try:
            for followup in followups:
                prior = Path(known[followup["id"]]["artifacts"])
                previous = worker.read_json(prior / "report.json")
                session = worker.read_json(prior / "session.json")
                if (previous["execution"]["termination"] != "exited" or previous["execution"]["exit_code"] != 0
                        or previous.get("runtime_errors") or not previous.get("session_identity_ok")
                        or previous.get("usage") is None or not session.get("session_id")
                        or session.get("resumed_to")):
                    raise ValueError("Worker did not finish in a safely resumable state.")
            for followup in followups:
                ident = followup["id"]
                continuation_started = True
                repair_count += 1
                resumed = worker.resume_task(Path(known[ident]["artifacts"]), output / "followups" / ident,
                                             followup["prompt"], timeout, env=env,
                                             _held_roots=(worker.git_root(cwd),), _excluded=(output,))
                usage_parts.append(resumed)
                known[ident] = worker.task_summary(resumed)
                batch["tasks"] = list(known.values())
                batch["usage"] = worker.total_usage(usage_parts)
                batch["artifacts"] = str(output / "followups")
                batch.setdefault("scope_violations", []).extend(resumed.get("scope_violations", []))
                batch["status"] = "completed" if all(t["status"] == "completed" for t in known.values()) and not batch["scope_violations"] else "failed"
                worker.write_json(output / "followups/summary.json", batch)
                continuation_started = False
                if resumed["status"] == "failed":
                    raise ValueError("Worker continuation failed; inspect its artifacts.")
        except (ValueError, OSError, KeyError) as error:
            if continuation_started:
                batch["usage"] = None  # An exception may have hidden a started invocation.
                batch["status"] = "failed"
                worker.write_json(output / "followups/summary.json", batch)
            review["issues"].append(str(error))
            break
    report = final["final"]
    valid = plan["valid"] and final["valid"] and worker.validate_report(report)
    status = report["status"] if valid else "failed"
    if status == "completed" and batch["status"] != "completed":
        status = "failed"
    result = dict(status=status,
                  summary=report["summary"][:1200] if valid else "Main review failed; inspect artifacts.",
                  main_valid=valid, main_session_id=final["events"]["session_id"],
                  # The resumed session total already includes planning; do not add it twice.
                  main_usage=final["events"]["usage"] if valid else None,
                  worker_usage=batch["usage"] if batch else None,
                  repair_invocations=repair_count,
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
