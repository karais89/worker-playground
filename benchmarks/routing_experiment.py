"""Fixed three-arm routing experiment; no additional production runner features.

Three existing fixtures x two repeats, plus one pinned repository task x one
repeat. Run only inside a freshly provisioned benchmark environment.
"""
import argparse
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

BASELINE = "20fb8cb"


def git_file(name):
    return subprocess.check_output(["git", "show", f"{BASELINE}:{name}"], cwd=ROOT)


def repository_case():
    names = ["worker.py", "team.py", "bench.py", "README.md",
             "tests/test_worker.py", "tests/test_team.py", "tests/test_bench.py"]
    files = {name: git_file(name).decode("utf-8") for name in names}
    grade = '''
import inspect, tempfile
from pathlib import Path
from unittest.mock import patch
import subprocess, sys
import team, worker
assert inspect.signature(team.run_team).parameters['concurrency'].default == 3
batch_signature = inspect.signature(worker.run_batch)
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    task = dict(id='code', role='implement', scope=['a.py'], prompt='task', acceptance=[])
    def phase(final):
        return dict(valid=True, final=final, execution={'exit_code':0},
                    events={'usage':{'total_tokens':100}, 'session_id':'main'})
    report = dict(status='completed',summary='done',changed_files=[],checks=[],issues=[])
    for limit in (1, 2, 3):
        with patch.object(team, 'invoke', side_effect=[phase({'tasks':[task]}), phase(report)]), \
             patch.object(worker, 'run_batch', return_value=dict(status='completed',usage=None,tasks=[])) as batch:
            result = team.run_team('task', root, root / f'out-{limit}', concurrency=limit)
            assert result['status'] == 'completed'
            assert batch_signature.bind(*batch.call_args.args, **batch.call_args.kwargs).arguments['concurrency'] == limit
    for limit in (0, 4):
        with patch.object(team, 'invoke') as invoke:
            try: team.run_team('task', root, root / f'bad-{limit}', concurrency=limit)
            except ValueError: pass
            else: raise AssertionError('invalid concurrency accepted')
            invoke.assert_not_called()
    prompt = root / 'task.txt'
    prompt.write_text('task')
    signature = inspect.signature(team.run_team)
    with patch.object(team, 'run_team', return_value=dict(status='completed',worker_status='completed')) as run:
        assert team.main([str(prompt), '--cwd', str(root), '--concurrency', '2']) == 0
        assert signature.bind(*run.call_args.args, **run.call_args.kwargs).arguments['concurrency'] == 2
subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests'], check=True, capture_output=True)
'''
    # Existing tests are part of the acceptance contract, not editable grading data.
    for name in names:
        if name.startswith("tests/"):
            grade += f"\nassert Path({name!r}).read_text(encoding='utf-8') == {files[name]!r}\n"
    return dict(files=files, grade=grade, prompt=(
        "In this CLI worker repository, expose the existing worker concurrency control through team.py. "
        "Add --concurrency and a concurrency keyword argument to run_team, both defaulting to 3. "
        "Accept 1..3 and forward the selected limit to worker.run_batch. Invalid limits must be rejected "
        "before any model invocation. Preserve existing behavior and usage accounting. Update README usage. "
        "Keep existing test files unchanged, add regression tests in a new root-level test_*.py file, "
        "and run both the existing tests (python -m unittest discover -s tests) and new tests."
    ))


def comparisons(rows):
    result = {}
    for label in ("legacy", "team"):
        selected = [dict(r, arm="team" if r["arm"] == label else "solo")
                    for r in rows if r["arm"] in ("solo", label)]
        result[label] = bench.summarize(selected, False)["comparisons"]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--auth-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--isolated-environment", required=True)
    args = parser.parse_args()
    # Task prompts name `python`, so validate that exact command before spending
    # model tokens. A python3-only image otherwise measures command recovery too.
    subprocess.run(["python", "-c", "import sys,unittest; assert sys.version_info >= (3, 11)"], check=True)
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    args.codex = "codex"
    args.main_model = args.worker_model = "gpt-6.1-sol"
    args.effort = args.worker_effort = "high"
    args.timeout = 600
    args.diagnostic = False
    baseline_path = args.output / "baseline_team.py"
    baseline_path.write_bytes(git_file("team.py"))
    assert git_file("worker.py") == (ROOT / "worker.py").read_bytes(), "worker must stay fixed in this ablation"
    spec = importlib.util.spec_from_file_location("baseline_team", baseline_path)
    legacy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(legacy)
    bench.CASES["repository"] = repository_case()
    manifest = dict(source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
                    baseline_commit=subprocess.check_output(['git','rev-parse',BASELINE],cwd=ROOT,text=True).strip(),
                    cli=subprocess.check_output(['codex','--version'],text=True).strip(),
                    prompt_python=subprocess.check_output(['python','--version'],text=True).strip(),
                    python=sys.version, isolated_environment=args.isolated_environment,
                    model=args.main_model, effort=args.effort,
                    source_sha256={p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest()
                                   for p in ('team.py','worker.py','bench.py','benchmarks/routing_experiment.py')},
                    fixture_sha256=hashlib.sha256(json.dumps(bench.CASES,sort_keys=True).encode()).hexdigest(),
                    protocol='bug/modules/investigate: solo,legacy,team then reverse; repository: solo,legacy,team once')
    worker.write_json(args.output / "manifest.json", manifest)
    rows = []
    for name in bench.CASES:
        for repeat in range(1 if name == "repository" else 2):
            for variant in (("solo", "legacy", "team") if repeat == 0 else ("team", "legacy", "solo")):
                print(f"Running {name} {variant} repeat {repeat+1}", flush=True)
                bench.team = legacy if variant == "legacy" else team
                out = args.output / f"{name}-{repeat+1}-{variant}"
                row = bench.run_one(name, "solo" if variant == "solo" else "team", out, args)
                row["arm"] = variant
                phase_paths = [out / 'events.jsonl'] if variant == 'solo' else sorted((out/'coordination').glob('*/events.jsonl'))
                row['main_phases'] = {p.parent.name: {'commands': len(worker.parse_events(p)['commands']),
                                                      'cumulative_usage': worker.parse_events(p)['usage']} for p in phase_paths}
                worker.write_json(out / "result.json", row)
                rows.append(row)
                worker.write_json(args.output / "summary.json", dict(runs=rows, comparisons=comparisons(rows)))
                print(json.dumps({k:row[k] for k in ('case','arm','valid','passed','main_usage','worker_invocations')}), flush=True)
    return 0 if all(bench.eligible(row) for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
