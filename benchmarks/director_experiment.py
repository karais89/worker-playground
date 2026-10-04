"""Fixed comparison of solo, routing-only, and bounded-feedback directors.

Run in a freshly provisioned OS environment. Both team.py and worker.py are
loaded from the pinned previous version; the fixtures and grader stay common.
No production prompts or eligibility rules are changed for this experiment.
"""
import argparse
from contextlib import contextmanager
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import bench
import team
import worker
from benchmarks.routing_experiment import repository_case

PREVIOUS = "2ee466a3031be70b190780d7b43f076564aa1646"
CURRENT = "cba3439"


def load_previous(output):
    modules = {}
    for name in ("worker", "team"):
        path = output / ("previous_" + name + ".py")
        path.write_bytes(subprocess.check_output(["git", "show", f"{PREVIOUS}:{name}.py"], cwd=ROOT))
        spec = importlib.util.spec_from_file_location("previous_" + name, path)
        module = importlib.util.module_from_spec(spec)
        # The old director must import the old worker, including its report format.
        saved = sys.modules["worker"]
        try:
            sys.modules["worker"] = modules.get("worker", worker)
            spec.loader.exec_module(module)
        finally:
            sys.modules["worker"] = saved
        modules[name] = module
    assert modules["team"].worker is modules["worker"]
    return modules


def comparisons(rows):
    return {label: bench.summarize([
        dict(r, arm="team" if r["arm"] == label else "solo")
        for r in rows if r["arm"] in ("solo", label)
    ], False)["comparisons"] for label in ("previous", "current")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--auth-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--isolated-environment", required=True)
    args = parser.parse_args()
    subprocess.run(["python", "-c", "import sys,unittest; assert sys.version_info >= (3,11)"], check=True)
    for name in ("team.py", "worker.py"):
        assert (ROOT / name).read_bytes() == subprocess.check_output(
            ["git", "show", f"{CURRENT}:{name}"], cwd=ROOT), "Production source changed"
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    args.codex = "codex"
    args.main_model = args.worker_model = "gpt-6.1-sol"
    args.effort = args.worker_effort = "high"
    args.timeout = 600
    args.diagnostic = False
    previous = load_previous(args.output)
    bench.CASES["repository"] = repository_case()
    schedule = [(case, repeat + 1, arm) for case in bench.CASES
                for repeat in range(1 if case == "repository" else 2)
                for arm in (("solo", "previous", "current") if repeat == 0
                            else ("current", "previous", "solo"))]
    manifest = dict(current_commit=subprocess.check_output(["git", "rev-parse", CURRENT], cwd=ROOT, text=True).strip(),
                    previous_commit=PREVIOUS, model=args.main_model, effort=args.effort,
                    cli=subprocess.check_output(["codex", "--version"], text=True).strip(),
                    python=sys.version, isolated_environment=args.isolated_environment,
                    source_sha256={name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                                   for name in ("team.py", "worker.py", "bench.py", "benchmarks/director_experiment.py")},
                    fixture_sha256=hashlib.sha256(json.dumps(bench.CASES, sort_keys=True).encode()).hexdigest(),
                    schedule=schedule, eligibility="unchanged bench.eligible; historical failed invocation is not erased by recovery",
                    stop_rule="stop immediately on workspace credits or usage limit errors")
    worker.write_json(args.output / "manifest.json", manifest)
    original_profile = bench.runtime_profile

    @contextmanager
    def measured_profile(output):
        with original_profile(output) as home:
            try:
                yield home
            finally:
                # Keep only policy metadata, never authentication or full rollouts.
                contexts = []
                for path in (home / ".codex/sessions").rglob("*.jsonl"):
                    session_id = None
                    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                        try:
                            item = json.loads(line)
                        except ValueError:
                            continue
                        payload = item.get("payload", {})
                        if item.get("type") == "session_meta":
                            session_id = payload.get("id")
                        if item.get("type") == "turn_context":
                            contexts.append(dict(session_id=session_id, **{key: payload.get(key) for key in
                                ("model", "effort", "sandbox_policy", "approval_policy")}))
                worker.write_json(output / "turn-contexts.json", contexts)

    bench.runtime_profile = measured_profile
    rows, stopped = [], None
    for case, repeat, arm in schedule:
        print(f"Running {case} {arm} repeat {repeat}", flush=True)
        bench.team = previous["team"] if arm == "previous" else team
        bench.worker = previous["worker"] if arm == "previous" else worker
        output = args.output / f"{case}-{repeat}-{arm}"
        row = bench.run_one(case, "solo" if arm == "solo" else "team", output, args)
        row.update(arm=arm, repeat=repeat)
        paths = [output / "events.jsonl"] if arm == "solo" else sorted((output / "coordination").glob("*/events.jsonl"))
        row["main_phases"] = {p.parent.name: {key: worker.parse_events(p)[key]
                                             for key in ("session_id", "commands", "usage", "errors")}
                              for p in paths}
        summary = output / "coordination/summary.json"
        row["repair_invocations"] = worker.read_json(summary).get("repair_invocations", 0) if summary.exists() else 0
        row["turn_contexts"] = worker.read_json(output / "turn-contexts.json")
        row["eligible"] = bench.eligible(row)
        worker.write_json(output / "result.json", row)
        rows.append(row)
        for events in output.rglob("events.jsonl"):
            text = events.read_text(encoding="utf-8", errors="replace").lower()
            if "out of credits" in text or "usage limit" in text:
                stopped = "CLI credit/usage limit; remaining scheduled runs not attempted"
                break
        worker.write_json(args.output / "summary.json", dict(runs=rows, comparisons=comparisons(rows),
                          stopped_reason=stopped, remaining_schedule=schedule[len(rows):]))
        print(json.dumps({key: row[key] for key in ("case", "arm", "repeat", "eligible", "passed", "main_usage",
                                                    "worker_invocations", "repair_invocations")}), flush=True)
        if stopped:
            break
    return 0 if len(rows) == len(schedule) and all(bench.eligible(r) for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
