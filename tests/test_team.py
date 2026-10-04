from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

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

    def test_zero_worker_plan_still_finishes_in_main(self):
        plan = self.phase({"tasks": []}, 100)
        final = self.phase(dict(status="completed", summary="done", changed_files=[], checks=[], issues=[]), 140)
        with patch.object(team, "invoke", side_effect=[plan, final]), patch.object(team.worker, "run_batch") as run_batch:
            result = team.run_team("tiny task", self.repo, self.root / "output")
        run_batch.assert_not_called()
        self.assertEqual(result["status"], "completed")
        self.assertFalse(result["actual_delegation"])

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
        phases = [self.phase({"tasks": []}, 100), self.phase(dict(
            status="completed", summary="done", changed_files=[], checks=[], issues=[]), 140)]
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


if __name__ == "__main__":
    unittest.main()
