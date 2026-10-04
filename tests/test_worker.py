import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import worker

FAKE_CLI = r'''
import json, pathlib, sys, time
args = sys.argv[1:]
prompt = sys.stdin.buffer.read().decode("utf-8")
data = json.loads(next(line[6:] for line in prompt.splitlines() if line.startswith("Task: ")))
start = time.time()
sid = args[args.index("resume")+1] if "resume" in args else "test-session"
print(json.dumps({"type":"thread.started","thread_id":sid}), flush=True)
time.sleep(data.get("delay",0))
for path, text in data.get("files",{}).items():
    p=pathlib.Path(path); p.parent.mkdir(parents=True,exist_ok=True); p.write_text(text,encoding="utf-8")
final={"status":"completed","summary":json.dumps({"start":start,"end":time.time(),"prompt":prompt},ensure_ascii=False),"changed_files":list(data.get("files",{})),"checks":[],"issues":[]}
if not data.get("no_report"):
    pathlib.Path(args[args.index("--output-last-message")+1]).write_text(json.dumps(final,ensure_ascii=False),encoding="utf-8")
usage={"input_tokens":30 if "resume" in args else 10,"cached_input_tokens":8 if "resume" in args else 4,"output_tokens":7 if "resume" in args else 3,"reasoning_output_tokens":4 if "resume" in args else 2}
print(json.dumps({"type":"turn.completed","usage":None if data.get("no_usage") else usage}),flush=True)
sys.exit(data.get("exit",0))
'''


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cli-worker-test-")
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.fake = self.root / "fake.py"
        self.fake.write_text(FAKE_CLI, encoding="utf-8")
        self.cli = [sys.executable, str(self.fake)]

    def tearDown(self):
        self.temp.cleanup()

    def task(self, ident="a", scope=None, role="implement", **settings):
        return dict(id=ident, role=role, cwd=str(self.repo), scope=scope or [ident + ".txt"],
                    prompt=json.dumps(settings, ensure_ascii=False), acceptance=["correct output"])

    def batch(self, tasks, **kwargs):
        return worker.run_batch(tasks, self.root / "out", cli=self.cli, **kwargs)

    def test_usage_excludes_duplicate_cache_and_reasoning(self):
        result = self.batch([self.task(files={"a.txt": "안녕"})])
        self.assertEqual(result["usage"]["total_tokens"], 13)
        self.assertEqual(result["usage"]["cached_input_tokens"], 4)
        self.assertEqual(result["tasks"][0]["observed_changed_files"], ["a.txt"])
        self.assertIn("안녕", (self.root / "out/a/changes.patch").read_text(encoding="utf-8"))

    def test_parallel_disjoint_scopes(self):
        result = self.batch([self.task("a", delay=.35), self.task("b", delay=.35)])
        spans = [json.loads(row["summary"]) for row in result["tasks"]]
        self.assertLess(max(s["start"] for s in spans), min(s["end"] for s in spans))

    def test_overlapping_scopes_serialized_in_input_order(self):
        result = self.batch([self.task("a", scope=["shared"], delay=.15),
                             self.task("b", scope=["shared/x.py"], delay=.15)])
        a, b = [json.loads(row["summary"]) for row in result["tasks"]]
        self.assertGreaterEqual(b["start"], a["end"])

    def test_review_waits_for_writer(self):
        result = self.batch([self.task("a", scope=["a.txt"], delay=.15),
                             self.task("b", scope=["."], role="review")])
        a, b = [json.loads(row["summary"]) for row in result["tasks"]]
        self.assertGreaterEqual(b["start"], a["end"])

    def test_failure_does_not_hide_successful_sibling(self):
        result = self.batch([self.task("a", exit=4), self.task("b")])
        self.assertEqual(result["status"], "failed")
        self.assertEqual([r["status"] for r in result["tasks"]], ["failed", "completed"])

    def test_missing_usage_not_zero(self):
        result = self.batch([self.task(no_usage=True)])
        self.assertIsNone(result["usage"])

    def test_missing_final_report_fails(self):
        self.assertEqual(self.batch([self.task(no_report=True)])["status"], "failed")

    def test_timeout(self):
        result = self.batch([self.task(delay=4)], timeout=.2)
        saved = worker.read_json(self.root / "out/a/report.json")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(saved["execution"]["termination"], "timeout")
        self.assertIsNone(result["usage"])

    def test_resume_identity_and_usage_are_separate(self):
        self.batch([self.task()])
        session = worker.read_json(self.root / "out/a/session.json")
        result = worker.resume_task(self.root / "out/a", self.root / "resume", "한국어 후속 지시", 10)
        self.assertTrue(result["session_identity_ok"])
        self.assertEqual(result["usage"]["total_tokens"], 24)
        self.assertEqual(result["cumulative_usage"]["total_tokens"], 37)
        self.assertIn("한국어 후속 지시", (self.root / "resume/prompt.txt").read_text(encoding="utf-8"))
        self.assertEqual(worker.read_json(self.root / "out/a/report.json")["usage"]["total_tokens"], 13)
        with self.assertRaises(ValueError):
            worker.resume_task(self.root / "out/a", self.root / "stale", "stale", 10)

    def test_directory_scope_contains_files_on_windows(self):
        result = self.batch([self.task(scope=["pkg"], files={"pkg/file.py": "answer = 42\n"})])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["tasks"][0]["observed_changed_files"], ["pkg/file.py"])

    def test_nested_cwd_cannot_hide_outside_change(self):
        sub = self.repo / "pkg"
        sub.mkdir()
        task = self.task(files={"../outside.txt": "oops"})
        task["cwd"] = str(sub)
        self.assertTrue(self.batch([task])["scope_violations"])

    def test_unknown_or_reset_cumulative_usage_not_zero(self):
        self.assertIsNone(worker.usage_delta(None, {}))
        self.assertIsNone(worker.usage_delta(dict(input_tokens=1, cached_input_tokens=0, output_tokens=1),
                                           dict(input_tokens=10, cached_input_tokens=0, output_tokens=3)))

    def test_out_of_scope_change_detected(self):
        result = self.batch([self.task(files={"outside.txt": "oops"})])
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["scope_violations"])

    def test_invalid_scopes_and_duplicate_ids(self):
        for value in ("../escape", "C:/escape", "/escape", "*.py"):
            with self.assertRaises(ValueError):
                worker.normalize_scope(value)
        spec = self.root / "tasks.json"
        worker.write_json(spec, {"tasks": [self.task(), self.task()]})
        with self.assertRaises(ValueError):
            worker.load_tasks(spec)

    def test_double_terminal_usage_rejected(self):
        log = self.root / "events.jsonl"
        record = json.dumps({"type": "turn.completed", "usage": {
            "input_tokens": 10, "cached_input_tokens": 2, "output_tokens": 3}})
        log.write_text(record + "\n" + record + "\n", encoding="utf-8")
        self.assertIsNone(worker.parse_events(log)["usage"])

    def test_workspace_lock_rejects_second_owner(self):
        with worker.directory_lock(self.repo):
            with self.assertRaises(ValueError):
                with worker.directory_lock(self.repo):
                    self.fail("lock was shared")


if __name__ == "__main__":
    unittest.main()
