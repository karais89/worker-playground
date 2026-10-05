"""Paired host diagnostic: Codex main with Codex versus Hive/OpenCode workers."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import bench
import opencode_backend as adapter
import team
import worker
from benchmarks.project_cases import cases
from benchmarks.validate_project_cases import validate

HIVE_MODEL = "hive-ai/deepseek-ai/deepseek-v4.1-flash"
MAIN_MODEL = "gpt-6.1-sol"


def source_settings(cli):
    """Load only the selected provider in memory. Never print resolved config or auth."""
    result = subprocess.run(cli + ["debug", "config"], capture_output=True, text=True,
                            encoding="utf-8", timeout=45, check=True)
    provider = json.loads(result.stdout).get("provider", {}).get("hive-ai")
    if not isinstance(provider, dict) or "deepseek-ai/deepseek-v4.1-flash" not in provider.get("models", {}):
        raise ValueError("Hive DeepSeek v4.1 Flash is missing from the local OpenCode configuration")
    paths = subprocess.run(cli + ["debug", "paths"], capture_output=True, text=True,
                           encoding="utf-8", timeout=45, check=True)
    data = next((line.split(None, 1)[1] for line in paths.stdout.splitlines() if line.startswith("data ")), None)
    if data is None:
        raise ValueError("Cannot locate the OpenCode credential store")
    credentials = worker.read_json(Path(data) / "auth.json")
    if "hive-ai" not in credentials:
        raise ValueError("Hive is not authenticated in OpenCode")
    # No other provider, plugins, skills or user instructions enter the new profile.
    return provider, {"hive-ai": credentials["hive-ai"]}


@contextmanager
def profile(output, codex_auth, settings):
    with bench.runtime_profile(output) as home:
        env = bench.clean_environment(home, codex_auth)
        env["XDG_STATE_HOME"] = str(home / "state")
        provider, auth = settings
        target = home / "data/opencode/auth.json"
        worker.write_json(target, auth)
        if os.name != "nt":
            target.chmod(0o600)
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps({
            "enabled_providers": ["hive-ai"], "provider": {"hive-ai": provider},
            "model": HIVE_MODEL, "autoupdate": False, "share": "disabled",
        })
        yield home, env


def contexts(home):
    found = []
    for path in (home / ".codex/sessions").rglob("*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            item = json.loads(line)
            if item.get("type") == "turn_context":
                payload = item.get("payload", {})
                found.append({key: payload.get(key) for key in ("model", "effort")})
    return found


def opencode_contexts(cli, env, output):
    found = []
    for path in sorted(output.rglob("session.json")):
        session = worker.read_json(path)
        if session.get("backend") != "opencode" or not session.get("session_id"):
            continue
        try:
            # Large native CLI exports can be truncated when stdout is a pipe.
            # Keep the transcript in an automatically removed private file and
            # retain only model/usage metadata in the benchmark artifacts.
            with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as transcript:
                exported = subprocess.run(cli + ["export", session["session_id"]], env=env,
                                          stdout=transcript, stderr=subprocess.PIPE, text=True,
                                          encoding="utf-8", timeout=45)
                if exported.returncode:
                    found.append(dict(session_id=session["session_id"], exported=False))
                    continue
                transcript.seek(0)
                data = json.load(transcript)
            models = [{key: message["info"].get(key) for key in ("modelID", "providerID", "variant", "finish", "tokens")}
                      for message in data.get("messages", []) if message.get("info", {}).get("role") == "assistant"]
            found.append(dict(session_id=session["session_id"], exported=True, messages=models))
        except ValueError:
            found.append(dict(session_id=session["session_id"], exported=False))
    return found


def run_case(case_name, case, backend, output, args, settings):
    output.mkdir(parents=True, exist_ok=False)
    repo = output / "project"
    bench.prepare_case(repo, case)
    with profile(output, args.auth_file, settings) as (home, env):
        started = time.monotonic()
        result = team.run_team(case["prompt"] + bench.TEST_INSTRUCTION, repo, output / "coordination",
                               cli=[args.codex], main_model=MAIN_MODEL,
                               worker_model=HIVE_MODEL if backend == "opencode" else MAIN_MODEL,
                               effort="high", worker_effort="max" if backend == "opencode" else "high",
                               timeout=args.timeout, env=env, worker_backend=backend,
                               worker_cli=args.opencode if backend == "opencode" else None)
        seconds = round(time.monotonic() - started, 3)
        grading = bench.grade(repo, case)
        worker.write_json(output / "grade.json", grading)
        reports = [worker.read_json(p) for p in (output / "coordination").rglob("report.json")]
        valid = result.get("main_valid", False) and result["status"] == "completed"
        row = dict(case=case_name, backend=backend, status=result["status"], valid=valid,
                   passed=grading["passed"], seconds=seconds, main_usage=result.get("main_usage"),
                   worker_usage=result.get("worker_usage"), actual_delegation=result.get("actual_delegation", False),
                   worker_invocations=len(reports), repair_invocations=result.get("repair_invocations", 0),
                   worker_failures=sum(r["status"] != "completed" for r in reports),
                   model_contexts=contexts(home), opencode_contexts=opencode_contexts(args.opencode, env, output),
                   artifacts=str(output))
        row["eligible"] = (valid and grading["passed"] and row["main_usage"] is not None
                           and not row["worker_failures"]
                           and (not row["actual_delegation"] or row["worker_usage"] is not None))
        runtime_errors = [err for report in reports for err in report.get("runtime_errors", [])]
        for path in (output / "coordination").glob("*/events.jsonl"):
            runtime_errors += worker.parse_events(path)["errors"]
        row["quota_error"] = any(any(word in err.lower() for word in (
            "out of credits", "usage limit", "insufficient", "quota", "rate limit", "429")) for err in runtime_errors)
    row["profile_removed"] = not home.exists()
    worker.write_json(output / "result.json", row)
    return row


def summarize(rows, schedule, stopped=None, environment="Host diagnostic, fresh profiles; no fresh OS per run"):
    comparisons = []
    for name in sorted({r["case"] for r in rows}):
        selected = [r for r in rows if r["case"] == name]
        groups = {backend: [r for r in selected if r["backend"] == backend] for backend in adapter.BACKENDS}
        comparable = (all(groups.values()) and len(groups["codex"]) == len(groups["opencode"])
                      and all(r["eligible"] and r["actual_delegation"] for r in selected))
        entry = dict(case=name, comparable=bool(comparable),
                     passed={b: sum(r["passed"] for r in group) for b, group in groups.items()},
                     runs={b: len(group) for b, group in groups.items()})
        for metric in ("seconds", "main_tokens", "worker_tokens", "total_tokens"):
            values = {}
            for backend, group in groups.items():
                points = []
                for row in group:
                    main = row["main_usage"]["total_tokens"] if row["main_usage"] else None
                    wrk = row["worker_usage"]["total_tokens"] if row["worker_usage"] else None
                    point = row["seconds"] if metric == "seconds" else (
                        main if metric == "main_tokens" else wrk if metric == "worker_tokens" else
                        main + wrk if main is not None and wrk is not None else None)
                    if point is not None:
                        points.append(point)
                values[backend] = statistics.median(points) if points else None
            entry[metric + "_median"] = values
        # Keep failure metrics visible, but never calculate a savings claim from them.
        a, b = entry["seconds_median"]["codex"], entry["seconds_median"]["opencode"]
        entry["opencode_time_change"] = b / a - 1 if comparable and a else None
        comparisons.append(entry)
    return dict(diagnostic=True, environment=environment,
                runs=rows, comparisons=comparisons, stopped_reason=stopped,
                remaining_schedule=schedule[len(rows):],
                note="Two exploratory repeats. Different worker models, tokenizers and reasoning settings; tokens are not costs or equal work units.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--auth-file", type=Path,
                        default=Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "auth.json")
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--opencode", default="opencode")
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--environment-label", default="Windows host, fresh profiles; no OS isolation")
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--case", choices=["all", *cases()], default="all")
    args = parser.parse_args()
    args.output = args.output.resolve()
    args.opencode = adapter.resolve_cli([args.opencode])
    settings = source_settings(args.opencode)
    args.output.mkdir(parents=True, exist_ok=False)
    fixture_cases = cases()
    if args.case != "all":
        fixture_cases = {args.case: fixture_cases[args.case]}
    schedule = [(name, repeat + 1, backend) for name in fixture_cases for repeat in range(2)
                for backend in (("codex", "opencode") if repeat == 0 else ("opencode", "codex"))]
    if args.preflight_only:
        fixture_cases = {"preflight": dict(files={"probe.py": "VALUE = 0\n"},
            prompt="Change probe.VALUE to 42. Add a unittest for this exact value and run it. Scope is probe.py and test_probe.py.",
            grade="from probe import VALUE\nassert VALUE == 42")}
        schedule = [("preflight", 1, "opencode")]
    controls = validate() if not args.preflight_only else None
    manifest = dict(source_commit=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                    source_sha256={p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in
                                   ("team.py", "worker.py", "opencode_backend.py", "bench.py",
                                    "benchmarks/backend_comparison.py", "benchmarks/project_cases.py",
                                    "benchmarks/validate_project_cases.py")},
                    main_model=MAIN_MODEL, main_effort="high", worker_models={"codex": MAIN_MODEL, "opencode": HIVE_MODEL},
                    worker_efforts={"codex": "high", "opencode": "max"}, timeout=args.timeout,
                    cli_versions={"codex": subprocess.check_output([args.codex, "--version"], text=True).strip(),
                                  "opencode": subprocess.check_output(args.opencode + ["--version"], text=True).strip()},
                    python=sys.version, platform=sys.platform, environment=args.environment_label,
                    schedule=schedule, grader_controls=controls, preflight=args.preflight_only,
                    fixture_sha256=hashlib.sha256(json.dumps(fixture_cases, sort_keys=True).encode()).hexdigest(),
                    provider_model_options={key: settings[0]["models"]["deepseek-ai/deepseek-v4.1-flash"].get("options", {}).get(key)
                                            for key in ("reasoningEffort", "max_tokens")},
                    stopping="Stop on quota or runtime failure; preserve all runs. No silent retries or model substitutions.")
    worker.write_json(args.output / "manifest.json", manifest)
    rows, stopped = [], None
    for name, repeat, backend in schedule:
        print(f"Running {name} {backend} repeat {repeat}", flush=True)
        row = run_case(name, fixture_cases[name], backend, args.output / f"{name}-{repeat}-{backend}", args, settings)
        row["repeat"] = repeat
        rows.append(row)
        if row["quota_error"]:
            stopped = "quota/rate limit error"
        elif not row["valid"]:
            stopped = "runtime or coordinator failure"
        worker.write_json(args.output / "summary.json", summarize(rows, schedule, stopped, args.environment_label))
        print(json.dumps({key: row[key] for key in ("case", "backend", "status", "passed", "eligible", "seconds", "repair_invocations")}), flush=True)
        if stopped:
            break
    return 0 if len(rows) == len(schedule) and all(r["eligible"] for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
