"""Two-arm live CLI simulation on a synthetic multi-file SQLite application."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import bench
import worker
from benchmarks.project_cases import cases
from benchmarks.validate_project_cases import validate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--auth-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--isolated-environment", required=True)
    args = parser.parse_args()
    subprocess.run(["python", "-c", "import sqlite3, unittest; print(sqlite3.sqlite_version)"], check=True)
    validation = validate()
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    args.codex = "codex"
    args.main_model = args.worker_model = "gpt-6.1-sol"
    args.effort = args.worker_effort = "high"
    args.timeout = 600
    args.diagnostic = False
    bench.CASES = cases()
    schedule = [(name, repeat + 1, arm) for name in bench.CASES for repeat in range(2)
                for arm in (("solo", "team") if repeat == 0 else ("team", "solo"))]
    names = ["worker.py", "team.py", "bench.py", "benchmarks/project_cases.py",
             "benchmarks/project_experiment.py", "benchmarks/validate_project_cases.py"]
    manifest = dict(source_commit=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                    source_sha256={name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in names},
                    fixture_sha256=hashlib.sha256(json.dumps(bench.CASES, sort_keys=True).encode()).hexdigest(),
                    model=args.main_model, worker_model=args.worker_model, effort=args.effort, timeout=args.timeout,
                    cli=subprocess.check_output(["codex", "--version"], text=True).strip(), python=sys.version,
                    environment=args.isolated_environment, schedule=schedule, grader_controls=validation,
                    scope="Synthetic 11-file TaskDesk simulation, not a real OSS repository or public benchmark",
                    stopping="Stop on CLI credit/usage limit errors; preserve other failures without retries")
    worker.write_json(args.output/"manifest.json", manifest)
    rows, stopped = [], None
    for name, repeat, arm in schedule:
        print(f"Running {name} {arm} repeat {repeat}", flush=True)
        output = args.output/f"{name}-{repeat}-{arm}"
        row = bench.run_one(name, arm, output, args)
        row["repeat"] = repeat
        row["eligible"] = bench.eligible(row)
        summary_path = output/"coordination/summary.json"
        coordinator = worker.read_json(summary_path) if summary_path.exists() else {}
        row["repair_invocations"] = coordinator.get("repair_invocations", 0)
        task_path = output/"coordination/tasks.json"
        tasks = worker.read_json(task_path)["tasks"] if task_path.exists() else []
        row["assignments"] = [{key: t[key] for key in ("id", "role", "scope")} for t in tasks]
        paths = sorted((output/"coordination").glob("*/events.jsonl")) if arm == "team" else [output/"events.jsonl"]
        row["main_phases"] = {p.parent.name: {key: worker.parse_events(p)[key]
                                             for key in ("session_id", "usage", "commands", "errors")} for p in paths}
        worker.write_json(output/"result.json", row)
        rows.append(row)
        for path in output.rglob("events.jsonl"):
            events = worker.parse_events(path)
            if any("out of credits" in err.lower() or "usage limit" in err.lower() for err in events["errors"]):
                stopped = "CLI credits or usage limit exhausted"
        summary = bench.summarize(rows, False)
        summary.update(stopped_reason=stopped, remaining_schedule=schedule[len(rows):])
        worker.write_json(args.output/"summary.json", summary)
        print(json.dumps({key: row[key] for key in ("case", "arm", "repeat", "passed", "eligible", "main_usage",
                                                    "worker_invocations", "repair_invocations")}), flush=True)
        if stopped:
            break
    return 0 if len(rows) == len(schedule) and all(r["eligible"] for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
