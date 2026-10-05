"""Opt-in, bounded role/model director. The legacy team.py remains unchanged.

Pure policy is testable without any installed AI CLI. Runtime reuses worker.py's
execution, permission, scope, continuation and usage primitives.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import threading
from typing import Any

ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,47}\Z")
MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./:-]{0,199}\Z")
ROLES = ("research", "implement", "review")
USAGE_KEYS = ("input_tokens", "cached_input_tokens", "output_tokens", "total_tokens")


@dataclass(frozen=True)
class Limits:
    stages: int = 3
    workers: int = 6
    concurrency: int = 3
    timeout: float = 600

    def __post_init__(self):
        for name, maximum in (("stages", 6), ("workers", 12), ("concurrency", 3)):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f"{name} must be an integer in 1..{maximum}")
        if (type(self.timeout) not in (int, float) or not math.isfinite(self.timeout)
                or self.timeout <= 0):
            raise ValueError("timeout must be positive and finite")


def profiles_from(value: Any) -> dict[str, dict]:
    """An operator-owned allowlist, never model-generated executable settings."""
    if not isinstance(value, dict) or set(value) != {"version", "profiles"} or type(value["version"]) is not int or value["version"] != 1:
        raise ValueError("profiles require version=1 and profiles")
    rows = value["profiles"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 8:
        raise ValueError("configure 1..8 profiles")
    found, seen = {}, set()
    for row in rows:
        keys = {"id", "backend", "model", "effort", "roles", "description", "enabled"}
        if not isinstance(row, dict) or set(row) != keys:
            raise ValueError("invalid profile fields")
        name = row["id"]
        if not isinstance(name, str) or not ID.fullmatch(name) or name in seen:
            raise ValueError("invalid or duplicate profile id")
        seen.add(name)
        if type(row["enabled"]) is not bool:
            raise ValueError("enabled must be boolean")
        if row["backend"] not in ("codex", "opencode"):
            raise ValueError("backend must be codex or opencode")
        if not isinstance(row["model"], str) or not MODEL.fullmatch(row["model"]):
            raise ValueError("invalid model id")
        if row["backend"] == "opencode" and "/" not in row["model"]:
            raise ValueError("OpenCode requires an explicit provider/model id")
        effort = row["effort"]
        if not isinstance(effort, str) or not re.fullmatch(r"[A-Za-z0-9_-]{0,32}", effort):
            raise ValueError("invalid effort/variant")
        if row["backend"] == "codex" and not effort:
            raise ValueError("Codex effort cannot be empty")
        roles = row["roles"]
        if (not isinstance(roles, list) or not roles or not all(isinstance(r, str) and r in ROLES for r in roles)
                or len(set(roles)) != len(roles)):
            raise ValueError("invalid roles")
        if not isinstance(row["description"], str) or len(row["description"]) > 500:
            raise ValueError("profile description must be at most 500 characters")
        if row["enabled"]:
            if "REPLACE" in row["model"]:
                raise ValueError("replace the example model id before enabling it")
            found[name] = dict(row)
    if not found:
        raise ValueError("enable at least one profile")
    return found


def sum_usage(items: list[dict]) -> dict | None:
    if not items or any(not isinstance(item.get("usage"), dict) for item in items):
        return None
    if any(type(item["usage"].get(k)) is not int or item["usage"][k] < 0 for item in items for k in USAGE_KEYS):
        return None
    return {k: sum(item["usage"][k] for item in items) for k in USAGE_KEYS}


def decision_schema(profiles: dict, report_schema: dict) -> dict:
    def obj(properties):
        return {"type": "object", "additionalProperties": False,
                "properties": properties, "required": list(properties)}
    text = {"type": "string"}
    task = obj({"id": text, "profile": {"type": "string", "enum": list(profiles)},
                "role": {"type": "string", "enum": list(ROLES)},
                "scope": {"type": "array", "items": text}, "prompt": text,
                "acceptance": {"type": "array", "items": text},
                "replaces": {"type": ["string", "null"]}})
    return obj({"summary": text, "tasks": {"type": "array", "maxItems": 3, "items": task},
                "followups": {"type": "array", "maxItems": 3,
                              "items": obj({"id": text, "prompt": text})},
                "report": {"anyOf": [report_schema, {"type": "null"}]}})


def validate_decision(value, profiles, known, repaired, replaced, stages, starts, limits):
    if not isinstance(value, dict) or set(value) != {"summary", "tasks", "followups", "report"}:
        raise ValueError("invalid director response fields")
    if not isinstance(value["summary"], str):
        raise ValueError("summary must be text")
    tasks, followups, report = value["tasks"], value["followups"], value["report"]
    if not isinstance(tasks, list) or not isinstance(followups, list):
        raise ValueError("tasks and followups must be lists")
    if sum((bool(tasks), bool(followups), report is not None)) != 1:
        raise ValueError("choose exactly one: new tasks, followups, or final report")
    if tasks:
        if stages >= limits.stages or len(tasks) > 3 or starts + len(tasks) > limits.workers:
            raise ValueError("worker/stage budget exceeded")
        seen, targets = set(known), set()
        for task in tasks:
            if not isinstance(task, dict) or set(task) != {"id", "profile", "role", "scope", "prompt", "acceptance", "replaces"}:
                raise ValueError("invalid task fields")
            ident = task["id"]
            if not isinstance(ident, str) or not ID.fullmatch(ident) or ident in seen:
                raise ValueError("invalid or duplicate task id")
            seen.add(ident)
            profile = task["profile"]
            if not isinstance(profile, str) or profile not in profiles or task["role"] not in profiles[profile]["roles"]:
                raise ValueError("profile/role is not allowed")
            if not isinstance(task["prompt"], str) or not task["prompt"].strip():
                raise ValueError("task prompt is required")
            for key in ("scope", "acceptance"):
                if not isinstance(task[key], list) or not task[key] or not all(isinstance(s, str) and s.strip() for s in task[key]):
                    raise ValueError(f"nonempty {key} required")
            prior = task["replaces"]
            if prior is not None:
                if not isinstance(prior, str) or prior not in known or prior in replaced or prior in targets:
                    raise ValueError("invalid replacement target")
                old = known[prior]
                if old["result"]["status"] != "blocked":
                    raise ValueError("only a safely finished blocked task may be explicitly replaced")
                if any(task[k] != old["task"][k] for k in ("role", "scope", "acceptance")):
                    raise ValueError("replacement must preserve role, scope and acceptance")
                targets.add(prior)
    if followups:
        if len(followups) > 3:
            raise ValueError("at most three followups per decision")
        seen = set()
        for item in followups:
            if not isinstance(item, dict) or set(item) != {"id", "prompt"}:
                raise ValueError("invalid followup fields")
            ident = item["id"]
            if (not isinstance(ident, str) or ident not in known or ident in seen
                    or ident in repaired or ident in replaced):
                raise ValueError("unknown, repaired, or replaced worker")
            if not isinstance(item["prompt"], str) or not item["prompt"].strip():
                raise ValueError("followup prompt is required")
            seen.add(ident)
    return "tasks" if tasks else "followups" if followups else "report"


POLICY = """You are a bounded director, not a fixed agent pipeline. Minimize head work while preserving quality.
For a trivial known-location change you may finish directly on the FIRST turn only.
Otherwise delegate without broad head exploration; default to one end-to-end implementer.
Choose profile AND research/implement/review role for each task from the configured allowlist.
Do not assume a model is better from its name. Profile descriptions are operator preferences, not benchmarks.
Separate research or review only when it is needed. After receiving research, decide the next stage using its evidence.
Within one stage assign only logically independent work, or scope-ordered tasks. File disjointness alone is not proof of independence.
Return exactly one action: tasks, followups, or report; the other lists must be empty and report null unless final.
Use self-contained prompts, relative file/directory scopes (not globs), and observable acceptance criteria.
Use unique task IDs. A replacement must explicitly name a safely finished blocked task in replaces and preserve its role, scope and acceptance.
Never replace a runtime failure, silently substitute a model, or request access beyond the user's task.
Followups resume the SAME worker session/model/role/scope once; a review/research worker cannot edit or run shell commands in OpenCode.
After delegation remain READ-ONLY. Inspect necessary changed code and tests; do not repeat broad exploration, edit, run tests/builds, or commit.
Worker summaries are claims; observed command exits and changed code are evidence. A zero exit code is not proof of correctness.
For a blocking concern name the requirement, concrete scenario, expected behavior and evidence. Request reproduction if uncertain.
Bundle corrections; avoid speculative extra features or architectural preferences. Workers execute checks and corrections.
Do not spawn native subagents, other AI CLIs, or nested teams. The host owns scheduling and budgets.
No Jev, permanent advisors, unlimited repair loop, automatic commits or publishing.
Final completed requires all non-replaced tasks completed AND the original acceptance criteria verified; otherwise report blocked.
"""


def run_director(request, profiles, runtime, output, limits=Limits()):
    """runtime implements the IO boundary; no model calls occur during validation."""
    if not isinstance(request, str) or not request.strip():
        raise ValueError("request must be nonempty text")
    output = Path(output)
    known, invocations, replaced, repaired = {}, [], set(), set()
    session, stages, starts, main_usage = None, 0, 0, None
    report, status, error, head_calls = None, "failed", None, 0
    # One head turn per new stage or correction batch, plus the final review.
    max_decisions = limits.stages + limits.workers + 1
    runtime.prepare(profiles, limits)
    output.mkdir(parents=True, exist_ok=False)
    runtime.write(output / "profiles.json", {"version": 1, "profiles": list(profiles.values())})
    runtime.write(output / "limits.json", vars(limits))
    try:
        with runtime.lock():
            for turn in range(max_decisions):
                state = {"stages_left": limits.stages - stages, "worker_starts_left": limits.workers - starts,
                         "concurrency": limits.concurrency, "repaired": sorted(repaired),
                         "replaced": sorted(replaced), "final_turn": turn == max_decisions - 1}
                prompt = (POLICY + "\nOriginal request:\n" + request + "\nAvailable profiles:\n"
                          + json.dumps(list(profiles.values()), ensure_ascii=False)
                          + "\nBudget/state:\n" + json.dumps(state)
                          + "\nPrevious results:\n" + runtime.handoff(known, output))
                main_usage = None  # A failed next call must not leave a stale cumulative total.
                head_calls += 1
                frame = runtime.decide(prompt, decision_schema(profiles, runtime.report_schema),
                                       output / f"head-{turn:02d}", session, limits.timeout)
                # The CLI counter is cumulative for the SAME head session: never sum turns.
                main_usage = frame.get("usage")
                if not frame.get("valid") or not frame.get("session_id") or (session and frame["session_id"] != session):
                    raise ValueError("director invocation failed or changed session")
                session = frame["session_id"]
                value = frame["final"]
                action = validate_decision(value, profiles, known, repaired, replaced, stages, starts, limits)
                if action == "report":
                    report = value["report"]
                    if not runtime.valid_report(report):
                        raise ValueError("invalid final report")
                    status = report["status"]
                    if status == "completed" and any(v["result"]["status"] != "completed" for k, v in known.items() if k not in replaced):
                        status, error = "blocked", "An unresolved worker cannot be reported completed."
                    break
                if turn == max_decisions - 1:
                    raise ValueError("director decision budget exhausted; no unreviewed work is launched")
                if action == "tasks":
                    tasks = value["tasks"]
                    # Validate ALL scopes/paths before any worker is dispatched.
                    tasks = runtime.validate_tasks(tasks, output)
                    stage_dir = output / f"stage-{stages + 1:02d}"
                    stages += 1
                    starts += len(tasks)  # Reserve before dispatch, including interrupted/unknown calls.
                    offset = len(invocations)
                    invocations.extend({"id": t["id"], "profile": t["profile"], "usage": None,
                                        "kind": "start", "observed": False} for t in tasks)
                    result = runtime.dispatch(tasks, profiles, stage_dir, limits)
                    for index, (task, item) in enumerate(zip(tasks, result["tasks"], strict=True)):
                        if item.get("id") != task["id"]:
                            raise ValueError("worker result identity mismatch")
                        known[task["id"]] = {"task": task, "result": item}
                        invocations[offset + index].update(usage=item.get("usage"), observed=True)
                        if task["replaces"] is not None:
                            replaced.add(task["replaces"])
                    if result.get("scope_violations") or any(not runtime.resumable(v["result"]) for v in known.values()):
                        raise ValueError("worker execution, measurement or scope failure; no automatic fallback")
                else:
                    # Preflight the entire set before spending tokens on its first correction.
                    items = value["followups"]
                    for item in items:
                        runtime.validate_resume(known[item["id"]]["result"])
                    for item in items:
                        ident = item["id"]
                        old = known[ident]
                        repaired.add(ident)
                        invocations.append({"id": ident, "profile": old["task"]["profile"],
                                            "usage": None, "kind": "resume", "observed": False})
                        fixed = runtime.resume(old["result"], item["prompt"], output / "followups" / ident, limits.timeout)
                        if fixed.get("id") != ident:
                            raise ValueError("resumed worker identity mismatch")
                        old["result"] = fixed
                        invocations[-1].update(usage=fixed.get("usage"), observed=True)
                        if not runtime.resumable(fixed):
                            raise ValueError("correction failed; no automatic retry or model fallback")
            else:
                error = "director decision budget exhausted"
    except Exception as exc:
        status, error = "failed", f"{type(exc).__name__}: {exc}"
    except KeyboardInterrupt:
        status, error = "failed", "cancelled"
    grouped = {}
    for name in profiles:
        rows = [v for v in invocations if v["profile"] == name]
        if rows:
            grouped[name] = {"backend": profiles[name]["backend"], "model": profiles[name]["model"],
                             "usage": sum_usage(rows), "invocations": len(rows)}
    summary = {"status": status, "error": error, "main_report": report, "main_usage": main_usage,
               "worker_usage_by_profile": grouped, "worker_usage": sum_usage(invocations),
               "worker_starts": starts, "stages": stages, "repairs": len(repaired), "head_calls": head_calls,
               "replaced": sorted(replaced), "invocations": invocations,
               "tasks": known, "artifacts": str(output),
               "counts_note": "Worker starts/stages/repairs count reserved budget slots, not confirmed model calls.",
               "measurement_note": "CLI tokens only; not subscription quota, prices or comparable model work."}
    runtime.write(output / "summary.json", summary)
    return summary


class Runtime:
    """Adapter over the existing runner; never changes global CLI configuration."""
    def __init__(self, cwd, *, main_model, effort="high", codex="codex", opencode="opencode", env=None):
        import worker
        import team
        self.w, self.team = worker, team
        self.cwd = Path(cwd).resolve()
        self.root = worker.git_root(self.cwd)
        self.main_model, self.effort, self.env = main_model, effort, env
        self.cli = {"codex": [codex], "opencode": [opencode]}
        self.report_schema = worker.REPORT_SCHEMA
        self.write, self.valid_report = worker.write_json, worker.validate_report

    def prepare(self, profiles, limits):
        for profile in profiles.values():
            self.w.opencode_backend.validate_backend(profile["backend"], profile["model"])
            if profile["backend"] == "opencode":
                self.cli["opencode"] = self.w.opencode_backend.resolve_cli(self.cli["opencode"])
                for role in profile["roles"]:
                    self.w.opencode_backend.prepare_env(self.env, role)

    def lock(self):
        return self.w.directory_lock(self.root)

    def decide(self, prompt, schema, out, session, timeout):
        result = self.team.invoke(self.cli["codex"], self.cwd, out, prompt, schema,
                                  self.main_model, self.effort, timeout, self.env, session_id=session)
        self.write(out / "execution.json", result["execution"])
        return {"valid": result["valid"], "session_id": result["events"]["session_id"],
                "usage": result["events"]["usage"], "final": result["final"]}

    def validate_tasks(self, tasks, output):
        path = output / "pending-tasks.json"
        self.write(path, {"tasks": [dict(t, cwd=str(self.cwd)) for t in tasks]})
        normalized = self.w.load_tasks(path)  # Existing path/symlink/scope validator.
        return [dict(clean, profile=raw["profile"], replaces=raw["replaces"])
                for clean, raw in zip(normalized, tasks, strict=True)]

    def handoff(self, known, output):
        rows = [dict(self.w.task_summary(v["result"]), profile=v["task"]["profile"],
                     scope=v["task"]["scope"], acceptance=v["task"]["acceptance"])
                for v in reversed(list(known.values()))]
        state = {"status": "observed", "tasks": rows, "artifacts": str(output / "history")}
        self.write(output / "history" / "summary.json", state)
        return self.w.main_report(state)  # Same 6,000-byte bound; full evidence stays on disk.

    @staticmethod
    def resumable(result):
        execution = result.get("execution", {})
        return (result.get("status") in ("completed", "blocked")
                and execution.get("termination") == "exited" and execution.get("exit_code") == 0
                and not result.get("runtime_errors") and result.get("session_identity_ok") is True
                and result.get("usage") is not None and not result.get("scope_violations"))

    def validate_resume(self, result):
        if not self.resumable(result):
            raise ValueError("worker is not safely resumable")
        prior = Path(result["artifacts"])
        saved = self.w.read_json(prior / "session.json")
        if not saved.get("session_id") or saved.get("resumed_to"):
            raise ValueError("worker has no unused continuation")
        backend = saved.get("backend", "codex")
        self.w.opencode_backend.validate_backend(backend, saved["model"])
        if saved["task"]["id"] != result["id"]:
            raise ValueError("saved worker identity does not match the result")
        if backend == "opencode":
            if saved.get("opencode_profile") != self.w.opencode_backend.profile_identity(self.env):
                raise ValueError("OpenCode profile changed before continuation")
            self.w.opencode_backend.resolve_cli(saved["cli"])
            self.w.opencode_backend.prepare_env(self.env, saved["task"]["role"])
        elif saved.get("codex_home") != (self.env if self.env is not None else os.environ).get("CODEX_HOME"):
            raise ValueError("CODEX_HOME changed before continuation")

    def resume(self, result, prompt, output, timeout):
        return self.w.resume_task(Path(result["artifacts"]), output, prompt, timeout,
                                  env=self.env, _held_roots=(self.root,), _excluded=(output.parents[1],))

    def dispatch(self, tasks, profiles, output, limits):
        """One shared scope audit, one scheduler; task-local models may differ."""
        output.mkdir(parents=True, exist_ok=False)
        batch = [dict(t, cwd=str(self.cwd)) for t in tasks]
        self.write(output / "tasks.json", {"tasks": batch})
        excluded = (output.parent,)
        before = self.w.snapshot(self.root, ["."], excluded)
        # Two OpenCode processes share a SQLite store even with different models.
        if sum(profiles[t["profile"]]["backend"] == "opencode" for t in tasks) > 1 and limits.concurrency > 1:
            folder = output / "initialization"
            command = [*self.cli["opencode"], "session", "list", "--format", "json", "--max-count", "0"]
            self.write(folder / "command.json", command)
            init = self.w.run_process(command, self.cwd, "", folder, min(limits.timeout, 45),
                                      env=self.w.opencode_backend.prepare_env(self.env, "implement"))
            self.write(folder / "execution.json", init)
            if init["termination"] != "exited" or init["exit_code"] != 0:
                raise ValueError(f"OpenCode initialization failed; inspect {folder}")
        pending, running, results, cancel = list(batch), {}, {}, threading.Event()
        with ThreadPoolExecutor(max_workers=limits.concurrency) as pool:
            try:
                while pending or running:
                    for task in pending[:]:
                        if len(running) >= limits.concurrency:
                            break
                        earlier = pending[:pending.index(task)]
                        if any(self.w.conflict(task, other) for other in [*running.values(), *earlier]):
                            continue
                        pending.remove(task)
                        p = profiles[task["profile"]]
                        future = pool.submit(self.w.execute_task, task, output / task["id"],
                                             self.cli[p["backend"]], p["model"], p["effort"],
                                             limits.timeout, cancel, env=self.env, excluded=excluded, backend=p["backend"])
                        running[future] = task
                    done, _ = wait(running, return_when=FIRST_COMPLETED)
                    for future in done:
                        task = running.pop(future)
                        try:
                            result = future.result()
                        except Exception as exc:
                            result = {"id": task["id"], "status": "failed", "usage": None,
                                      "summary": str(exc), "artifacts": str(output / task["id"])}
                        result["profile"] = task["profile"]
                        results[task["id"]] = result
                        self.write(output / task["id"] / "report.json", result)
            except BaseException:
                cancel.set()
                raise
        violations = self.w.scope_violations(self.root, before, self.w.snapshot(self.root, ["."], excluded), batch)
        result = {"tasks": [results[t["id"]] for t in tasks], "scope_violations": violations, "artifacts": str(output)}
        self.write(output / "summary.json", result)
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request", type=Path)
    parser.add_argument("--profiles", type=Path, required=True)
    parser.add_argument("--cwd", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--main-model", required=True)
    parser.add_argument("--effort", default="high")
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--opencode", default="opencode")
    parser.add_argument("--max-stages", type=int, default=3)
    parser.add_argument("--max-workers", type=int, default=6)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=600)
    args = parser.parse_args(argv)
    try:
        limits = Limits(args.max_stages, args.max_workers, args.concurrency, args.timeout)
        profiles = profiles_from(json.loads(args.profiles.read_text(encoding="utf-8-sig")))
        if not MODEL.fullmatch(args.main_model) or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", args.effort):
            raise ValueError("invalid main model/effort")
        output = args.output.resolve()
        runtime = Runtime(args.cwd, main_model=args.main_model, effort=args.effort,
                          codex=args.codex, opencode=args.opencode)
        if output.is_relative_to(runtime.root):
            raise ValueError("output must be outside the target Git worktree")
        result = run_director(args.request.read_text(encoding="utf-8-sig"), profiles, runtime, output, limits)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "completed" else 1
    except (ValueError, OSError) as exc:
        parser.exit(2, f"director: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
