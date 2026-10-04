from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import json

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import team
import worker


class TeamTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)

    def tearDown(self):
        self.temp.cleanup()

    def phase(self, final, tokens, valid=True):
        if isinstance(final, dict) and "status" in final:
            final.setdefault("followups", [])
        return dict(valid=valid, final=final, execution={"exit_code": 0},
                    events={"usage": {"total_tokens": tokens}, "session_id": "main-session"})

    def test_workers_run_on_host_and_main_resumes_without_double_counting(self):
        plan = self.phase({"tasks": [dict(id="code", role="implement", scope=["a.py"],
                                         prompt="Implement", acceptance=["works"])]}, 100)
        final = self.phase(dict(status="completed", summary="done", changed_files=["a.py"], checks=[], issues=[]), 160)
        batch = dict(status="completed", usage={"total_tokens": 500}, tasks=[])
        with patch.object(team, "invoke", side_effect=[plan, final]) as invoke, \
             patch.object(team.worker, "run_batch", return_value=batch) as run_batch:
            result = team.run_team("task", self.repo, self.root / "output")
        self.assertEqual(result["main_usage"]["total_tokens"], 160)
        self.assertEqual(result["worker_usage"]["total_tokens"], 500)
        self.assertEqual(invoke.call_args.kwargs["session_id"], "main-session")
        self.assertEqual(run_batch.call_args.args[0][0]["cwd"], str(self.repo.resolve()))
        self.assertTrue(result["actual_delegation"])

    def test_direct_completion_uses_only_one_main_turn(self):
        report = dict(status="completed", summary="done", changed_files=[], checks=[], issues=[])
        plan = self.phase({"tasks": [], "report": report}, 100)
        with patch.object(team, "invoke", return_value=plan) as invoke, patch.object(team.worker, "run_batch") as run_batch:
            result = team.run_team("tiny task", self.repo, self.root / "output")
        self.assertEqual(invoke.call_count, 1)
        run_batch.assert_not_called()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["main_usage"]["total_tokens"], 100)
        self.assertFalse(result["actual_delegation"])

    def test_empty_tasks_without_report_is_not_completion(self):
        with patch.object(team, "invoke", return_value=self.phase({"tasks": [], "report": None}, 100)) as invoke:
            result = team.run_team("task", self.repo, self.root / "output")
        self.assertEqual(invoke.call_count, 1)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["main_valid"])

    def test_direct_blocked_report_preserves_status(self):
        report = dict(status="blocked", summary="Need specification", changed_files=[], checks=[], issues=["ambiguous"])
        with patch.object(team, "invoke", return_value=self.phase({"tasks": [], "report": report}, 100)):
            result = team.run_team("task", self.repo, self.root / "output")
        self.assertEqual(result["status"], "blocked")

    def test_mixed_direct_report_and_delegation_is_rejected(self):
        report = dict(status="completed", summary="done", changed_files=[], checks=[], issues=[])
        task = dict(id="code", role="implement", scope=["a.py"], prompt="task", acceptance=[])
        with patch.object(team, "invoke", return_value=self.phase({"tasks": [task], "report": report}, 100)), \
             patch.object(team.worker, "run_batch") as run_batch:
            result = team.run_team("task", self.repo, self.root / "output")
        run_batch.assert_not_called()
        self.assertEqual(result["status"], "failed")

    def test_failed_plan_does_not_dispatch_or_report_success(self):
        with patch.object(team, "invoke", return_value=self.phase(None, None, False)), \
             patch.object(team.worker, "run_batch") as run_batch:
            result = team.run_team("task", self.repo, self.root / "output")
        run_batch.assert_not_called()
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["main_valid"])

    def test_lock_covers_planning_and_review_including_zero_workers(self):
        other_temp = self.root / "other-temp"
        other_temp.mkdir()
        phases = [self.phase({"tasks": [], "report": dict(
            status="completed", summary="done", changed_files=[], checks=[], issues=[])}, 100)]
        def inspect_lock(*args, **kwargs):
            script = "import worker; from pathlib import Path\nwith worker.directory_lock(Path(" + repr(str(self.repo.resolve())) + ")): pass"
            contender = subprocess.run([sys.executable, "-c", script], cwd=Path(team.__file__).parent,
                                       env=dict(os.environ, TMP=str(other_temp), TEMP=str(other_temp), TMPDIR=str(other_temp)),
                                       capture_output=True)
            self.assertNotEqual(contender.returncode, 0)
            self.assertIn(b"another runner owns", contender.stderr)
            return phases.pop(0)
        with patch.object(team, "invoke", side_effect=inspect_lock):
            team.run_team("task", self.repo, self.root / "output")
        with worker.directory_lock(self.repo.resolve()):
            pass  # Released after the complete team lifecycle.

    def test_coordinator_reuses_its_lock_for_real_empty_cost_batch(self):
        task = dict(id="code", role="implement", scope=["a.py"], prompt="task", acceptance=[])
        phases = [self.phase({"tasks": [task]}, 100), self.phase(dict(
            status="completed", summary="done", changed_files=[], checks=[], issues=[]), 140)]
        report = dict(id="code", role="implement", status="completed", usage=None, summary="done")
        with patch.object(team, "invoke", side_effect=phases), \
             patch.object(worker, "execute_task", return_value=report):
            result = team.run_team("task", self.repo, self.root / "output")
        self.assertEqual(result["worker_status"], "completed")

    def feedback_fixture(self, second_followup=False, first_usage=True):
        fake = self.root / "fake-worker.py"
        fake.write_text('''import json, os, pathlib, sys
args = sys.argv[1:]
prompt = sys.stdin.read()
assert os.environ.get('WORKER_TEST_ENV') == 'preserved'
resumed = 'resume' in args
sid = args[args.index('resume') + 1] if resumed else 'worker-session'
pathlib.Path('a.py').write_text('fixed' if resumed else 'draft')
if resumed and 'escape' in prompt: pathlib.Path('outside.py').write_text('bad')
report = dict(status='completed', summary='worker done', changed_files=['a.py'], checks=[], issues=[])
pathlib.Path(args[args.index('--output-last-message') + 1]).write_text(json.dumps(report))
print(json.dumps(dict(type='thread.started', thread_id=sid)))
print(json.dumps(dict(type='item.completed',item=dict(type='command_execution',command='fixture-check',exit_code=0))))
usage = dict(input_tokens=30 if resumed else 10,cached_input_tokens=8 if resumed else 4,output_tokens=7 if resumed else 3)
if not resumed and os.environ.get('MISSING_USAGE'): usage = None
print(json.dumps(dict(type='turn.completed', usage=usage)))
''', encoding="utf-8")
        task = dict(id="code", role="implement", scope=["a.py"], prompt="Implement", acceptance=["fixed"])
        report = dict(status="blocked", summary="Needs correction", changed_files=[], checks=[], issues=[],
                      followups=[dict(id="code", prompt="Correct the defect and run relevant tests")])
        final = dict(status="completed", summary="Verified", changed_files=["a.py"], checks=[], issues=[])
        if second_followup:
            final = dict(report)
        phases = [self.phase({"tasks": [task], "report": None}, 100),
                  self.phase(report, 160), self.phase(final, 210)]
        env = dict(os.environ, CODEX_HOME=str(self.root / "profile"), WORKER_TEST_ENV="preserved")
        if not first_usage:
            env["MISSING_USAGE"] = "1"
        return [sys.executable, str(fake)], phases, env

    def test_feedback_reuses_real_worker_session_environment_and_delta_usage(self):
        cli, phases, env = self.feedback_fixture()
        out = self.repo / "artifacts"
        with patch.object(team, "invoke", side_effect=phases) as invoke:
            result = team.run_team("task", self.repo, out, cli=cli, env=env)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(invoke.call_count, 3)
        self.assertEqual(result["main_usage"]["total_tokens"], 210)
        self.assertEqual(result["worker_usage"]["total_tokens"], 37)  # 13 initial + 24 continuation.
        self.assertEqual(result["repair_invocations"], 1)
        self.assertEqual((self.repo / "a.py").read_text(), "fixed")
        original = worker.read_json(out / "workers/code/session.json")
        resumed = worker.read_json(out / "followups/code/session.json")
        self.assertEqual(original["session_id"], resumed["session_id"])
        self.assertEqual(original["task"], resumed["task"])
        self.assertEqual(original["model"], resumed["model"])
        self.assertEqual(original["resumed_to"], str(out / "followups/code"))
        summary = worker.read_json(out / "followups/summary.json")
        self.assertFalse(summary["scope_violations"])
        self.assertEqual(summary["tasks"][0]["observed_commands"][0]["exit_code"], 0)
        self.assertEqual(summary["artifacts"], str(out / "followups"))
        with worker.directory_lock(self.repo):
            pass

    def test_second_feedback_request_stops_at_budget(self):
        cli, phases, env = self.feedback_fixture(second_followup=True)
        with patch.object(team, "invoke", side_effect=phases) as invoke:
            result = team.run_team("task", self.repo, self.root / "out", cli=cli, env=env)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["repair_invocations"], 1)
        self.assertEqual(invoke.call_count, 3)

    def test_unmeasured_worker_is_not_automatically_resumed(self):
        cli, phases, env = self.feedback_fixture(first_usage=False)
        with patch.object(team, "invoke", side_effect=phases) as invoke:
            result = team.run_team("task", self.repo, self.root / "out", cli=cli, env=env)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["repair_invocations"], 0)
        self.assertIsNone(result["worker_usage"])
        self.assertEqual(invoke.call_count, 2)

    def test_unknown_feedback_worker_is_rejected_before_resume(self):
        cli, phases, env = self.feedback_fixture()
        phases[1]["final"]["followups"][0]["id"] = "unknown"
        with patch.object(team, "invoke", side_effect=phases), patch.object(worker, "resume_task") as resume:
            result = team.run_team("task", self.repo, self.root / "out", cli=cli, env=env)
        resume.assert_not_called()
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["main_valid"])

    def test_feedback_scope_violation_is_not_success_or_retried(self):
        cli, phases, env = self.feedback_fixture()
        phases[1]["final"]["followups"][0]["prompt"] = "escape"
        with patch.object(team, "invoke", side_effect=phases) as invoke:
            result = team.run_team("task", self.repo, self.root / "out", cli=cli, env=env)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["worker_status"], "failed")
        self.assertEqual(result["repair_invocations"], 1)
        self.assertEqual(invoke.call_count, 2)

    def test_review_invocation_requests_read_only_sandbox(self):
        observed = []
        def run(command, cwd, prompt, out, timeout, **kwargs):
            observed.append(command)
            worker.write_json(out / "final.json", {})
            (out / "events.jsonl").write_text(json.dumps(dict(type="thread.started", thread_id="main")) + "\n" +
                json.dumps(dict(type="turn.completed", usage=dict(input_tokens=1,cached_input_tokens=0,output_tokens=1))))
            return dict(exit_code=0, termination="exited")
        with patch.object(worker, "run_process", side_effect=run):
            team.invoke(["codex"], self.repo, self.root / "plan", "prompt", team.PLAN_SCHEMA, "model", "high", 1, None)
            team.invoke(["codex"], self.repo, self.root / "review", "prompt", team.REVIEW_SCHEMA, "model", "high", 1, None, session_id="main")
        self.assertIn('sandbox_mode="workspace-write"', observed[0])
        self.assertIn('sandbox_mode="read-only"', observed[1])


if __name__ == "__main__":
    unittest.main()
