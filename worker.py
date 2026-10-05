"""Small, standard-library Codex/OpenCode CLI worker runner (Python 3.11+)."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from contextlib import ExitStack, contextmanager
import difflib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
import uuid

import opencode_backend

DEFAULT_MODEL = "gpt-6.1-sol"
MAIN_REPORT_LIMIT = 6000  # UTF-8 bytes; full reports remain on disk.
ROLES = {
    "research": "Investigate only. Do not change files. Return conclusions with file/line evidence.",
    "implement": "Implement the assigned change and run relevant checks. Stay in the assigned scope.",
    "review": "Review the completed change without editing files. Report actionable findings with evidence.",
}
REPORT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["status", "summary", "changed_files", "checks", "issues"],
    "properties": {
        "status": {"type": "string", "enum": ["completed", "blocked", "failed"]},
        "summary": {"type": "string"},
        "changed_files": {"type": "array", "items": {"type": "string"}},
        "checks": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["command", "exit_code"],
            "properties": {"command": {"type": "string"}, "exit_code": {"type": ["integer", "null"]}},
        }},
        "issues": {"type": "array", "items": {"type": "string"}},
    },
}


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def main_report(batch):
    """Bound the entire handoff, including file lists, issues and JSON escaping."""
    if batch is None:
        return "No workers were dispatched."
    text = json.dumps(batch, ensure_ascii=False)
    if len(text.encode("utf-8")) <= MAIN_REPORT_LIMIT:
        return text
    compact = dict(status=batch["status"], truncated=True,
                   details=str(Path(batch["artifacts"]) / "summary.json"),
                   tasks=[dict(id=t.get("id"), status=t["status"], summary=t.get("summary", "")[:240])
                          for t in batch["tasks"][:3]])
    text = json.dumps(compact, ensure_ascii=False)
    if len(text.encode("utf-8")) > MAIN_REPORT_LIMIT:
        compact["tasks"] = []
        text = json.dumps(compact, ensure_ascii=False)
    if len(text.encode("utf-8")) > MAIN_REPORT_LIMIT:
        raise ValueError("artifact path exceeds the main report budget")
    return text


def normalize_scope(value: str) -> str:
    value = value.replace("\\", "/")
    if not value or value.startswith("/") or ":" in value or any(c in value for c in "*?["):
        raise ValueError(f"scope must be a relative file/directory, not a glob: {value!r}")
    parts = [p for p in value.split("/") if p not in ("", ".")]
    if ".." in parts:
        raise ValueError("scope cannot escape cwd")
    return "/".join(parts) or "."


def contains(scope: str, path: str) -> bool:
    scope, path = (os.path.normcase(p).replace("\\", "/") for p in (scope, path))
    return scope == "." or path == scope or path.startswith(scope.rstrip("/") + "/")


def load_tasks(path: Path):
    spec = read_json(path)
    raw = spec.get("tasks") if isinstance(spec, dict) else None
    if not isinstance(raw, list) or not raw:
        raise ValueError("tasks must be a nonempty list")
    tasks, ids = [], set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("each task must be an object")
        ident = item.get("id", "")
        if not isinstance(ident, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,47}", ident):
            raise ValueError("task id must be 1-48 alphanumeric, underscore or hyphen characters")
        if ident in ids:
            raise ValueError(f"duplicate task id: {ident}")
        ids.add(ident)
        role = item.get("role", "implement")
        if role not in ROLES:
            raise ValueError(f"unknown role: {role}")
        cwd = Path(item.get("cwd", "."))
        cwd = (path.parent / cwd).resolve() if not cwd.is_absolute() else cwd.resolve()
        if not cwd.is_dir():
            raise ValueError(f"cwd does not exist: {cwd}")
        scopes = item.get("scope", ["."] if role != "implement" else None)
        if not isinstance(scopes, list) or not scopes or not all(isinstance(s, str) for s in scopes):
            raise ValueError(f"{ident}: implementation requires a nonempty scope list")
        scopes = [normalize_scope(s) for s in scopes]
        for scope in scopes:
            if not (cwd / scope).resolve().is_relative_to(cwd):
                raise ValueError("scope follows a link outside cwd")
        prompt, acceptance = item.get("prompt"), item.get("acceptance", [])
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError(f"{ident}: prompt is required")
        if not isinstance(acceptance, list) or not all(isinstance(s, str) for s in acceptance):
            raise ValueError("acceptance must be a list of strings")
        tasks.append(dict(id=ident, role=role, cwd=str(cwd), scope=scopes,
                          prompt=prompt, acceptance=acceptance))
    return tasks


def conflict(a, b):
    """A reader also waits for an overlapping writer; two readers can run together."""
    if a["role"] != "implement" and b["role"] != "implement":
        return False
    for left in a["scope"]:
        for right in b["scope"]:
            p, q = Path(a["cwd"], left).resolve(), Path(b["cwd"], right).resolve()
            if p == q or p.is_relative_to(q) or q.is_relative_to(p):
                return True
    return False


def build_command(cli, task, model, effort, output: Path, session_id=None, backend="codex"):
    opencode_backend.validate_backend(backend, model)
    if backend == "opencode":
        return opencode_backend.build_command(cli, task, model, effort, session_id)
    # Keep backend-specific command construction and event decoding in two functions.
    command = [*cli, "--no-daemon", "-C", task["cwd"], "exec"]
    if session_id:
        command += ["resume", session_id]
    command += ["--json", "--color", "never"] if not session_id else ["--json"]
    command += ["-m", model, "-c", f'model_reasoning_effort="{effort}"',
                "-c", "agents.enabled=false", "-c", 'approval_policy="never"',
                "-c", 'sandbox_mode="' + ("workspace-write" if task["role"] == "implement" else "read-only") + '"',
                "--output-schema", str(output / "schema.json"),
                "--output-last-message", str(output / "final.json"), "-"]
    return command


def task_prompt(task, followup=None):
    return "\n".join([
        "You are a CLI worker. " + ROLES[task["role"]],
        "Do not delegate or start other agents. Do not commit. Other workers may be active: do not revert their changes.",
        "Avoid progress narration. Return a concise final JSON report using the supplied schema.",
        "Only report checks you actually ran. Keep the summary under 1200 characters; cite evidence for issues.",
        "Scope (file/directory paths): " + json.dumps(task["scope"]),
        "Task: " + task["prompt"],
        "Acceptance: " + json.dumps(task["acceptance"], ensure_ascii=False),
        "Follow-up: " + followup if followup is not None else "",
    ])


def parse_events(path: Path, backend="codex"):
    """Codex usage is session cumulative; OpenCode usage is per invocation."""
    if backend == "opencode":
        return opencode_backend.parse_events(path)
    session_id, messages, commands, errors, usages = None, [], [], [], []
    malformed, completed = 0, 0
    if not path.exists():
        return dict(session_id=None, usage=None, completed_turns=0, errors=["missing events"], commands=[], messages=[])
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if not isinstance(event, dict):
            malformed += 1
            continue
        kind = event.get("type")
        if kind == "thread.started":
            session_id = event.get("thread_id")
        elif kind == "turn.completed":
            completed += 1
            usage = event.get("usage") or {}
            if not isinstance(usage, dict):
                continue
            keys = ("input_tokens", "cached_input_tokens", "output_tokens")
            if all(type(usage.get(k)) is int and usage[k] >= 0 for k in keys) and usage["cached_input_tokens"] <= usage["input_tokens"]:
                usages.append({k: usage[k] for k in keys} | {
                    "reasoning_output_tokens": usage.get("reasoning_output_tokens")})
        elif kind == "turn.failed":
            errors.append(event.get("error", {}).get("message", "turn failed"))
        elif kind == "item.completed":
            item = event.get("item", {})
            if item.get("type") == "command_execution":
                commands.append({"command": item.get("command"), "exit_code": item.get("exit_code")})
            elif item.get("type") == "agent_message":
                messages.append(item.get("text", ""))
    # A CLI invocation should emit exactly one completed turn. Ambiguity is not zero usage.
    usage = usages[0] if completed == 1 and len(usages) == 1 and not malformed else None
    if usage is not None:
        usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
        usage["uncached_input_tokens"] = usage["input_tokens"] - usage["cached_input_tokens"]
    return dict(session_id=session_id, usage=usage, completed_turns=completed,
                malformed_lines=malformed, errors=errors, commands=commands, messages=messages)


def usage_delta(current, previous):
    if current is None or previous is None:
        return None
    keys = ("input_tokens", "cached_input_tokens", "output_tokens")
    delta = {key: current[key] - previous[key] for key in keys}
    if any(v < 0 for v in delta.values()) or delta["cached_input_tokens"] > delta["input_tokens"]:
        return None
    delta["total_tokens"] = delta["input_tokens"] + delta["output_tokens"]
    delta["uncached_input_tokens"] = delta["input_tokens"] - delta["cached_input_tokens"]
    a, b = current.get("reasoning_output_tokens"), previous.get("reasoning_output_tokens")
    delta["reasoning_output_tokens"] = a - b if type(a) is int and type(b) is int and a >= b else None
    return delta


def stop_process(process):
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait()


def run_process(command, cwd, prompt, output, timeout, cancel=None, env=None):
    output.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with (output / "events.jsonl").open("wb") as stdout, (output / "stderr.log").open("wb") as stderr:
        try:
            process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                       stdout=stdout, stderr=stderr,
                                       start_new_session=os.name != "nt")
        except OSError as error:
            return dict(exit_code=None, termination="launch_failed", error=str(error), seconds=0)
        try:
            # A separate writer avoids deadlock on large stdin; communicate handles the pipe.
            data = prompt.encode("utf-8")
            while True:
                try:
                    process.communicate(input=data, timeout=0.2)
                    return dict(exit_code=process.returncode, termination="exited",
                                seconds=round(time.monotonic() - started, 3))
                except subprocess.TimeoutExpired:
                    data = None
                    if (cancel and cancel.is_set()) or time.monotonic() - started >= timeout:
                        stop_process(process)
                        return dict(exit_code=process.returncode,
                                    termination="cancelled" if cancel and cancel.is_set() else "timeout",
                                    seconds=round(time.monotonic() - started, 3))
        except BaseException:
            stop_process(process)
            raise


def snapshot(cwd: Path, scope, excluded=()):
    """Observe non-ignored files; scope is a scheduling contract, not an OS sandbox."""
    listed = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                            cwd=cwd, capture_output=True)
    if listed.returncode:
        raise ValueError(f"not a Git worktree: {cwd}")
    files = {}
    for name in set(listed.stdout.decode("utf-8").split("\0")):
        path = cwd / name
        if not name or not any(contains(s, name) for s in scope) or path.is_symlink():
            continue
        if any(path.resolve().is_relative_to(p) for p in excluded):
            continue
        if path.is_file():
            files[name] = path.read_bytes()
    return files


def git_root(cwd):
    result = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd, capture_output=True)
    if result.returncode:
        raise ValueError(f"not a Git worktree: {cwd}")
    return Path(result.stdout.decode("utf-8").strip()).resolve()


def scope_violations(root, before, after, tasks):
    allowed = [(Path(t["cwd"]) / s).resolve() for t in tasks if t["role"] == "implement" for s in t["scope"]]
    return [str(root / name) for name in before.keys() | after.keys()
            if before.get(name) != after.get(name)
            and not any((root / name).resolve().is_relative_to(p) for p in allowed)]


def save_diff(before, after, output):
    changed, patch = [], []
    for name in sorted(before.keys() | after.keys()):
        if before.get(name) == after.get(name):
            continue
        changed.append(name)
        old, new = before.get(name, b""), after.get(name, b"")
        if b"\0" in old or b"\0" in new:
            patch.append(f"Binary file changed: {name}\n")
        else:
            patch.extend(difflib.unified_diff(old.decode("utf-8", "replace").splitlines(keepends=True),
                                             new.decode("utf-8", "replace").splitlines(keepends=True),
                                             fromfile="a/" + name if name in before else "/dev/null",
                                             tofile="b/" + name if name in after else "/dev/null"))
    (output / "changes.patch").write_text("".join(patch), encoding="utf-8")
    return changed


@contextmanager
def directory_lock(key):
    # Git's metadata directory is shared even when callers use different TEMP/HOME profiles.
    result = subprocess.run(["git", "rev-parse", "--absolute-git-dir"], cwd=key, capture_output=True, check=True)
    path = Path(result.stdout.decode("utf-8").strip()) / "cli-worker.lock"
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise ValueError(f"another runner owns this workspace/session; inspect lock: {path}") from None
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(str(os.getpid()))
        yield
    finally:
        path.unlink(missing_ok=True)


def validate_report(report):
    return (isinstance(report, dict) and report.get("status") in ("completed", "blocked", "failed")
            and isinstance(report.get("summary"), str)
            and all(isinstance(report.get(k), list) for k in ("changed_files", "checks", "issues"))
            and all(isinstance(v, str) for k in ("changed_files", "issues") for v in report[k])
            and all(isinstance(c, dict) and isinstance(c.get("command"), str)
                    and (c.get("exit_code") is None or type(c.get("exit_code")) is int) for c in report["checks"]))


def execute_task(task, output, cli, model, effort, timeout, cancel=None, session_id=None, followup=None, env=None,
                 previous_usage=None, excluded=(), backend="codex"):
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "task.json", task)
    write_json(output / "schema.json", REPORT_SCHEMA)
    command = build_command(cli, task, model, effort, output, session_id, backend)
    write_json(output / "command.json", command)
    prompt = task_prompt(task, followup)
    process_env = env
    if backend == "opencode":
        prompt += ("\nReturn ONLY one JSON object as your final response, without narration or Markdown. "
                   "It must match this JSON Schema:\n" + json.dumps(REPORT_SCHEMA))
        process_env = opencode_backend.prepare_env(env, task["role"])
    (output / "prompt.txt").write_text(prompt, encoding="utf-8")
    excluded = (*excluded, output.resolve())
    before = snapshot(Path(task["cwd"]), task["scope"], excluded)
    execution = run_process(command, task["cwd"], prompt, output, timeout, cancel, process_env)
    events = parse_events(output / "events.jsonl", backend)
    if backend == "opencode":
        usage = events["usage"]  # OpenCode events contain this invocation's steps, not session totals.
        cumulative = opencode_backend.add_usage(usage, previous_usage) if session_id else usage
        report = opencode_backend.final_report(events["messages"], REPORT_SCHEMA)
        if report is not None:
            write_json(output / "final.json", report)
    else:
        usage = usage_delta(events["usage"], previous_usage) if session_id else events["usage"]
        cumulative = events["usage"]
    after = snapshot(Path(task["cwd"]), task["scope"], excluded)
    changed = save_diff(before, after, output)
    if backend == "codex":
        try:
            report = read_json(output / "final.json")
        except (OSError, ValueError):
            report = None
    valid = validate_report(report)
    identity_ok = not session_id or events["session_id"] == session_id
    ok = (execution["termination"] == "exited" and execution["exit_code"] == 0
          and events["completed_turns"] == 1 and not events["errors"] and valid and identity_ok)
    result = dict(id=task["id"], role=task["role"], status=report["status"] if ok else "failed",
                  summary=report["summary"][:1200] if valid else "No valid final report; inspect artifacts.",
                  issues=report["issues"] if valid else [], observed_changed_files=changed,
                  worker_claims=report, observed_commands=events["commands"], execution=execution,
                  backend=backend, usage=usage, cumulative_usage=cumulative, measurement_complete=usage is not None,
                  session_identity_ok=identity_ok, artifacts=str(output),
                  runtime_errors=events["errors"])
    write_json(output / "report.json", result)
    write_json(output / "session.json", dict(session_id=events["session_id"], task=task,
                                              model=model, effort=effort, cli=cli, backend=backend,
                                              codex_home=(env if env is not None else os.environ).get("CODEX_HOME"),
                                              opencode_profile=opencode_backend.profile_identity(env) if backend == "opencode" else None,
                                              cumulative_usage=cumulative,
                                              previous_session_id=session_id))
    return result


def resume_task(prior, output, followup, timeout, *, env=None, _held_roots=(), _excluded=()):
    session = read_json(prior / "session.json")
    if not session.get("session_id"):
        raise ValueError("prior execution has no resumable session")
    backend = session.get("backend", "codex")  # Read artifacts produced before backend selection existed.
    opencode_backend.validate_backend(backend, session["model"])
    if backend == "opencode" and session.get("opencode_profile") != opencode_backend.profile_identity(env):
        raise ValueError("resume must use the same OpenCode profile environment as the original run")
    if backend == "opencode":
        opencode_backend.resolve_cli(session["cli"])
        opencode_backend.prepare_env(env, session["task"]["role"])
    if backend == "codex" and session.get("codex_home") != (env if env is not None else os.environ).get("CODEX_HOME"):
        raise ValueError("resume must use the same CODEX_HOME as the original run")
    if session.get("resumed_to"):
        raise ValueError("resume the newest result instead: " + session["resumed_to"])
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    root = git_root(session["task"]["cwd"])
    with ExitStack() as locks:
        if root not in _held_roots:
            locks.enter_context(directory_lock(root))
        # Check again under the lock, then reserve this link before spending tokens.
        session = read_json(prior / "session.json")
        if session.get("resumed_to"):
            raise ValueError("session already resumed: " + session["resumed_to"])
        if output.exists():
            raise ValueError(f"output already exists: {output}")
        excluded = (output, prior, *_excluded)
        before = snapshot(root, ["."], excluded)
        session["resumed_to"] = str(output)
        write_json(prior / "session.json", session)
        result = execute_task(session["task"], output, session["cli"], session["model"],
                              session["effort"], timeout, session_id=session["session_id"],
                              followup=followup, previous_usage=session.get("cumulative_usage"),
                              excluded=excluded, env=env, backend=backend)
        if result["execution"]["termination"] == "launch_failed":
            # No subprocess existed, so the old session and accounting remain untouched.
            session.pop("resumed_to")
            write_json(prior / "session.json", session)
        violations = scope_violations(root, before, snapshot(root, ["."], excluded), [session["task"]])
        result["scope_violations"] = violations
        if violations:
            result["status"] = "failed"
        write_json(output / "report.json", result)
        return result


def total_usage(results):
    if not results or any(r.get("usage") is None for r in results):
        return None
    return {key: sum(r["usage"][key] for r in results)
            for key in ("input_tokens", "cached_input_tokens", "output_tokens", "total_tokens")}


def task_summary(result):
    return {key: result.get(key) for key in ("id", "role", "status", "summary", "observed_changed_files",
                                           "observed_commands", "issues", "usage", "artifacts", "backend")}


def run_batch(tasks, output, cli=None, model=None, effort="high", concurrency=3, timeout=600, env=None,
              _held_roots=(), backend="codex"):
    if not 1 <= concurrency <= 3 or timeout <= 0:
        raise ValueError("concurrency must be 1..3 and timeout must be positive")
    model = DEFAULT_MODEL if model is None and backend == "codex" else model
    opencode_backend.validate_backend(backend, model)
    cli = cli or [backend]
    if backend == "opencode":
        cli = opencode_backend.resolve_cli(cli)
        opencode_backend.prepare_env(env, "implement")  # Fail before starting any tasks on invalid config.
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "tasks.json", {"tasks": tasks})
    cancel, results = threading.Event(), {}
    with ExitStack() as locks:
        roots = sorted({git_root(t["cwd"]) for t in tasks})
        for root in roots:
            if root not in _held_roots:  # Only the host coordinator passes its already-held lock.
                locks.enter_context(directory_lock(root))
        baselines = {root: snapshot(root, ["."], (output,)) for root in roots}
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            pending, running = list(tasks), {}
            try:
                while pending or running:
                    for task in pending[:]:
                        earlier = pending[:pending.index(task)]
                        if len(running) >= concurrency:
                            break
                        if any(conflict(task, other) for other in [*running.values(), *earlier]):
                            continue
                        pending.remove(task)
                        future = pool.submit(execute_task, task, output / task["id"], cli, model,
                                             effort, timeout, cancel, env=env, excluded=(output,), backend=backend)
                        running[future] = task
                    done, _ = wait(running, return_when=FIRST_COMPLETED)
                    for future in done:
                        task = running.pop(future)
                        try:
                            results[task["id"]] = future.result()
                        except Exception as error:
                            results[task["id"]] = dict(id=task["id"], status="failed", usage=None,
                                                       summary=str(error), backend=backend, artifacts=str(output / task["id"]))
                            write_json(output / task["id"] / "report.json", results[task["id"]])
            except KeyboardInterrupt:
                cancel.set()
                raise
        violations = []
        for root, before in baselines.items():
            violations.extend(scope_violations(root, before, snapshot(root, ["."], (output,)), tasks))
    ordered = [results[t["id"]] for t in tasks]
    summary = dict(status="completed" if all(r["status"] == "completed" for r in ordered) and not violations else "failed",
                   tasks=[task_summary(r) for r in ordered],
                   usage=total_usage(ordered), scope_violations=violations, artifacts=str(output), backend=backend)
    write_json(output / "summary.json", summary)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    run = sub.add_parser("run", help="run a JSON task list")
    run.add_argument("tasks", type=Path)
    run.add_argument("--model", help="Codex model (default: gpt-6.1-sol); explicit provider/model required for OpenCode")
    run.add_argument("--effort", default="high", help="Codex reasoning effort or OpenCode provider-specific variant")
    run.add_argument("--backend", choices=opencode_backend.BACKENDS, default="codex")
    run.add_argument("--concurrency", type=int, default=3)
    run.add_argument("--codex", default="codex", help="Codex executable path")
    run.add_argument("--opencode", default="opencode", help="OpenCode executable path")
    resume = sub.add_parser("resume", help="resume one prior task artifact directory")
    resume.add_argument("task_result", type=Path)
    resume.add_argument("followup", type=Path)
    for command in (run, resume):
        command.add_argument("--output", type=Path)
        command.add_argument("--timeout", type=float, default=600)
    args = parser.parse_args(argv)
    try:
        output = (args.output or Path(os.environ.get("CLI_WORKER_OUTPUT", "runs")) / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])).resolve()
        if args.action == "run":
            cli = [args.codex if args.backend == "codex" else args.opencode]
            result = run_batch(load_tasks(args.tasks.resolve()), output, cli, args.model,
                               args.effort, args.concurrency, args.timeout, backend=args.backend)
        else:
            prior = args.task_result.resolve()
            result = resume_task(prior, output, args.followup.read_text(encoding="utf-8-sig"), args.timeout)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "completed" else 1
    except (ValueError, OSError) as error:
        print(f"worker: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("worker: cancelled", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
