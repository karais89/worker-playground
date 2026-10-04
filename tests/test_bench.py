import copy
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bench
import worker


class BenchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bench-test-")
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_each_initial_fixture_fails_independent_grade(self):
        for name, case in bench.CASES.items():
            with self.subTest(case=name):
                repo = self.root / name
                bench.prepare_case(repo, case)
                self.assertFalse(bench.grade(repo, case)["passed"])
                status = subprocess.check_output(["git", "status", "--porcelain"], cwd=repo)
                self.assertEqual(status, b"")

    def test_reference_solutions_pass_grade(self):
        solutions = {
            "bug": {"flags.py": '''def parse_flag(value):
    if isinstance(value, bool): return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ('true', 'false'): return text == 'true'
    raise ValueError(value)
'''},
            "modules": {"slug.py": "import re\ndef slugify(text):\n    return re.sub('[^a-zA-Z0-9]+', '-', text).strip('-').lower()\n",
                        "stats.py": "def summarize(values):\n    return dict(count=len(values), min=min(values) if values else None, max=max(values) if values else None, mean=sum(values)/len(values) if values else None)\n"},
            "investigate": {"loader.py": '''from copy import deepcopy
from config import DEFAULTS
def merge(a, b):
    if not isinstance(a, dict) or not isinstance(b, dict): return deepcopy(b)
    result = deepcopy(a)
    for key, value in b.items(): result[key] = merge(result.get(key), value)
    return result
def load_config(overrides): return merge(DEFAULTS, overrides)
'''},
        }
        for name, files in solutions.items():
            repo = self.root / name
            bench.prepare_case(repo, bench.CASES[name])
            for path, contents in files.items():
                (repo / path).write_text(contents, encoding="utf-8")
            (repo / "test_solution.py").write_text("import unittest\nclass Tests(unittest.TestCase):\n    def test_contract(self):\n        exec(" + repr(bench.CASES[name]["grade"]) + ")\n", encoding="utf-8")
            self.assertTrue(bench.grade(repo, bench.CASES[name])["passed"], name)

    def test_correct_function_with_missing_or_failing_tests_is_rejected(self):
        repo = self.root / "case"
        case = dict(files={"solution.py": "VALUE = 42\n"}, grade="from solution import VALUE\nassert VALUE == 42")
        bench.prepare_case(repo, case)
        missing = bench.grade(repo, case)
        self.assertTrue(missing["functional"]["passed"])
        self.assertFalse(missing["passed"])
        (repo / "test_solution.py").write_text("import unittest\nclass Tests(unittest.TestCase):\n    def test_bad(self):\n        self.fail('deliberate failure')\n")
        failing = bench.grade(repo, case)
        self.assertTrue(failing["functional"]["passed"])
        self.assertFalse(failing["generated_tests"]["passed"])
        self.assertFalse(failing["passed"])

    def test_nonrecursive_deepcopy_and_update_mutant_is_rejected(self):
        repo = self.root / "mutant"
        bench.prepare_case(repo, bench.CASES["investigate"])
        (repo / "loader.py").write_text('''from copy import deepcopy
from config import DEFAULTS
def load_config(overrides):
    result = deepcopy(DEFAULTS)
    for key, value in overrides.items():
        if isinstance(result.get(key), dict) and isinstance(value, dict):
            result[key].update(deepcopy(value))
        else: result[key] = deepcopy(value)
    return result
''')
        self.assertFalse(bench.grade(repo, bench.CASES["investigate"])["functional"]["passed"])

    def rows(self):
        common = dict(case="bug", valid=True, passed=True, actual_delegation=True,
                      worker_usage={"total_tokens": 80}, worker_failures=0, worker_batch_failures=0)
        return [dict(common, arm="solo", main_usage={"total_tokens": 100}),
                dict(common, arm="team", main_usage={"total_tokens": 60})]

    def test_diagnostic_never_becomes_isolated_savings(self):
        comparison = bench.summarize(self.rows(), True)["comparisons"][0]
        self.assertAlmostEqual(comparison["diagnostic_reduction"], .4)
        self.assertIsNone(comparison["isolated_main_reduction"])

    def test_failures_or_missing_usage_invalidate_comparison(self):
        for key, value in (("passed", False), ("valid", False),
                           ("worker_usage", None), ("worker_failures", 1), ("worker_batch_failures", 1)):
            rows = copy.deepcopy(self.rows())
            rows[1][key] = value
            comparison = bench.summarize(rows, False)["comparisons"][0]
            self.assertIsNone(comparison["isolated_main_reduction"], key)

    def test_zero_worker_policy_is_eligible_without_claiming_delegation(self):
        rows = self.rows()
        rows[1].update(actual_delegation=False, worker_usage=None)
        comparison = bench.summarize(rows, False)["comparisons"][0]
        self.assertAlmostEqual(comparison["isolated_main_reduction"], .4)
        self.assertFalse(comparison["delegation_observed"])
        self.assertEqual(comparison["delegated_team_runs"], 0)
        self.assertTrue(bench.eligible(rows[1]))

    def test_profile_is_fresh_and_credentials_removed_even_on_failure(self):
        source = self.root / "auth-source.json"
        source.write_text('{"test": "fake-credential"}')
        profile = None
        with self.assertRaises(RuntimeError):
            with bench.runtime_profile(self.root) as profile:
                with patch.dict(os.environ, {"OPENAI_API_KEY": "must-not-inherit", "CODEX_HOME": "old-profile"}):
                    env = bench.clean_environment(profile, source)
                self.assertNotIn("OPENAI_API_KEY", env)
                self.assertEqual(env["CODEX_HOME"], str(profile / ".codex"))
                self.assertEqual((profile / ".codex/auth.json").read_text(), source.read_text())
                self.assertFalse((profile / ".codex/skills").exists())
                raise RuntimeError("intentional test failure")
        self.assertFalse(profile.exists())


if __name__ == "__main__":
    unittest.main()
