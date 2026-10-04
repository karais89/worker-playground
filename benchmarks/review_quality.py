"""Controlled live director review: three known defects and three clean controls.

Prepared implementations and intentionally incomplete passing tests are staged
at the batch boundary. Real worker sessions run those tests, then the unchanged
director reviews and may resume those same workers. Not natural error incidence
or an end-to-end comparison against solo development.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import bench
import team
import worker
from benchmarks.project_cases import cases, source
from benchmarks.validate_project_cases import reference

SMOKE = {
    "atomic_import": {"test_atomic_import.py": source('''
        import tempfile, unittest
        from pathlib import Path
        from taskdesk.storage import Store
        from taskdesk.service import TaskService
        class ImportTests(unittest.TestCase):
            def test_empty_import_preserves_existing(self):
                with tempfile.TemporaryDirectory() as tmp:
                    store = Store(Path(tmp)/'tasks.db')
                    try:
                        service = TaskService(store)
                        service.add_task('existing')
                        path = Path(tmp)/'data.json'; path.write_text('[]')
                        self.assertEqual(service.import_json(path), 0)
                        self.assertEqual([x.title for x in service.list_tasks()], ['existing'])
                    finally: store.close()
    ''')},
    "paged_listing": {"test_listing.py": source('''
        import unittest
        from taskdesk.storage import Store
        from taskdesk.service import TaskService
        class ListingTests(unittest.TestCase):
            def test_limit_and_sort(self):
                store = Store(':memory:')
                try:
                    service = TaskService(store)
                    service.add_task('low', priority=0)
                    service.add_task('high', priority=5)
                    self.assertEqual([t.title for t in service.list_tasks(sort='priority',limit=1)], ['high'])
                    self.assertEqual(service.list_tasks(limit=0), [])
                finally: store.close()
    ''')},
    "independent_utilities": {
        "test_csv_export.py": source('''
            import csv, io, unittest
            from taskdesk.domain import Task
            from taskdesk.reporting import to_csv
            class CsvTests(unittest.TestCase):
                def test_rows_and_empty_header(self):
                    self.assertEqual(to_csv([]), 'id,title,status,priority\\n')
                    rows = list(csv.reader(io.StringIO(to_csv(iter([Task(1,'a,b','open',2)])))))
                    self.assertEqual(rows[1], ['1','a,b','open','2'])
        '''),
        "test_settings.py": source('''
            import unittest
            from taskdesk.settings import load_settings
            class SettingsTests(unittest.TestCase):
                def test_explicit_environment_override(self):
                    self.assertEqual(load_settings(env={'TASKDESK_PAGE_SIZE':'8'})['report']['page_size'], 8)
                    with self.assertRaises(ValueError): load_settings(env={'TASKDESK_PAGE_SIZE':'0'})
        '''),
    },
}


def stage(name, repo, faulty):
    reference(name, repo)
    for filename, text in SMOKE[name].items():
        (repo/filename).write_text(text, encoding="utf-8")
    documentation = {
        "atomic_import": "JSON imports are atomic: a failed batch preserves all previously committed tasks and saves none of its own records.",
        "paged_listing": "Store.list_tasks and TaskService.list_tasks accept sort='id' or 'priority', limit=None, offset=0. Priority sorting is descending with id ascending as tie-breaker. CLI: python -m taskdesk --db tasks.db list --status open --sort priority --limit 10 --offset 5. Negative bounds and unknown sorts are rejected.",
        "independent_utilities": "reporting.to_csv(tasks) accepts a one-pass Task iterable and returns CSV with header id,title,status,priority and newline line endings. settings.load_settings(path=None, env=None) recursively merges object JSON into independent defaults. TASKDESK_PAGE_SIZE overrides report.page_size with an integer 1..1000. env=None uses process variables; env={} ignores them.",
    }
    path = repo/"README.md"
    path.write_text(path.read_text()+"\n"+documentation[name]+"\n", encoding="utf-8")
    if faulty:
        if name == "atomic_import":
            path = repo/"taskdesk/service.py"
            before, after = 'validate_record(record))', 'validate_record(record))\n                self.store.connection.commit()'
        elif name == "paged_listing":
            path = repo/"taskdesk/storage.py"
            before, after = 'limit, offset])', 'limit, 0])'
        else:
            path = repo/"taskdesk/settings.py"
            before, after = 'os.environ if env is None else env', 'env or os.environ'
        text = path.read_text(); assert before in text
        path.write_text(text.replace(before, after), encoding="utf-8")


def controls():
    results = []
    for name, case in cases().items():
        for faulty in (False, True):
            with tempfile.TemporaryDirectory() as tmp:
                repo = Path(tmp)/"project"
                bench.prepare_case(repo, case)
                stage(name, repo, faulty)
                grade = bench.grade(repo, case)
                assert grade["generated_tests"]["passed"], (name, faulty, grade)
                assert grade["functional"]["passed"] == (not faulty), (name, faulty, grade)
                results.append(dict(case=name, faulty=faulty, smoke_passed=True, external_passed=grade["functional"]["passed"]))
    return results


def run_case(name, faulty, output, args):
    case = cases()[name]
    output.mkdir(parents=True, exist_ok=False)
    repo = output/"project"
    bench.prepare_case(repo, case)
    original_batch = worker.run_batch
    before_grade = None

    def prepared_batch(tasks, batch_output, **kwargs):
        nonlocal before_grade
        stage(name, repo, faulty)
        before_grade = bench.grade(repo, case)
        assert before_grade["generated_tests"]["passed"]
        assert before_grade["functional"]["passed"] == (not faulty)
        worker.write_json(output/"before-grade.json", before_grade)
        (output/"before-review").mkdir()
        worker.save_diff({}, worker.snapshot(repo, ["."]), output/"before-review")
        # Only the initial worker task is controlled. The director sees the normal
        # requirement and real reports; it is not told whether a defect exists.
        handoff_tasks = [dict(t, prompt=(
            "For this INITIAL turn only, the implementation is already prepared in the workspace. "
            "Do not modify files or inspect implementation source. Run python -m unittest discover -s . "
            "and return a concise factual handoff report of the result. Do not speculate on correctness. "
            "For a later director follow-up, inspect and fix the requested issues within your assigned scope. "
            "The user requirements are:\n" + case["prompt"] + "\nYour assignment:\n" + t["prompt"])) for t in tasks]
        return original_batch(handoff_tasks, batch_output, **kwargs)

    with bench.runtime_profile(output) as home:
        env = bench.clean_environment(home, args.auth_file)
        with patch.object(worker, "run_batch", side_effect=prepared_batch):
            result = team.run_team(case["prompt"], repo, output/"coordination", ["codex"],
                                   "gpt-6.1-sol", "gpt-6.1-sol", "high", "high", 600, env)
        after = bench.grade(repo, case)
        worker.write_json(output/"after-grade.json", after)
        contexts = []
        for path in (home/".codex/sessions").rglob("*.jsonl"):
            for line in path.read_text().splitlines():
                event = json.loads(line)
                if event.get("type") == "turn_context":
                    payload = event["payload"]
                    contexts.append({k: payload.get(k) for k in ("model", "effort", "sandbox_policy")})
        worker.write_json(output/"turn-contexts.json", contexts)
    review_path = output/"coordination/review/final.json"
    review = worker.read_json(review_path) if review_path.exists() else None
    initial_reports = [worker.read_json(p) for p in (output/"coordination/workers").rglob("report.json")]
    contaminated = any(r["observed_changed_files"] for r in initial_reports)
    row = dict(case=name, faulty=faulty, before_grade=before_grade, after_grade=after,
               initial_review=review, coordinator=result, initial_worker_modified_code=contaminated,
               assignments=worker.read_json(output/"coordination/tasks.json") if (output/"coordination/tasks.json").exists() else None,
               artifacts=str(output), observed_contexts=contexts)
    worker.write_json(output/"result.json", row)
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--auth-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    validated = controls()
    args.output = args.output.resolve(); args.output.mkdir(parents=True, exist_ok=False)
    # Neutral run IDs avoid disclosing defect/control labels to either model.
    schedule = [("atomic_import", True), ("paged_listing", False), ("independent_utilities", True),
                ("atomic_import", False), ("paged_listing", True), ("independent_utilities", False)]
    worker.write_json(args.output/"manifest.json", dict(schedule=schedule, controls=validated,
        model="gpt-6.1-sol", effort="high", source_commit=subprocess.check_output(["git","rev-parse","HEAD"],cwd=ROOT,text=True).strip(),
        source_sha256={p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in
            ("team.py","worker.py","bench.py","benchmarks/review_quality.py","benchmarks/project_cases.py","benchmarks/validate_project_cases.py")},
        method="Prepared patches; real test-only initial worker sessions; unchanged director/feedback loop; 3 faulty and 3 clean controls, one each; no solo comparison"))
    rows, stopped = [], None
    for index, (name, faulty) in enumerate(schedule, 1):
        print(f"Running q{index:02d} {name} faulty={faulty}", flush=True)
        row = run_case(name, faulty, args.output/f"q{index:02d}", args)
        rows.append(row)
        for path in (args.output/f"q{index:02d}").rglob("events.jsonl"):
            if any("out of credits" in err.lower() or "usage limit" in err.lower() for err in worker.parse_events(path)["errors"]):
                stopped = "CLI credits or usage limit exhausted"
        worker.write_json(args.output/"summary.json", dict(runs=rows, stopped_reason=stopped, remaining_schedule=schedule[len(rows):]))
        print(json.dumps(dict(case=name,faulty=faulty,after_passed=row["after_grade"]["passed"],
                              repairs=row["coordinator"].get("repair_invocations"),status=row["coordinator"]["status"])),flush=True)
        if stopped: break


if __name__ == "__main__":
    main()
