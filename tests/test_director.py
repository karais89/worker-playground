"""Deterministic director policy/scheduler tests: no AI CLI or network calls."""
import copy
from contextlib import nullcontext
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import director as d


def profile(name="sol", backend="codex"):
    return dict(id=name, backend=backend, model="model" if backend == "codex" else "provider/model",
                effort="high", roles=list(d.ROLES), description="test", enabled=True)


def task(ident="a", name="sol", role="implement", replaces=None):
    return dict(id=ident, profile=name, role=role, scope=[ident + ".py"],
                prompt="Do the assigned task.", acceptance=["Observable behavior works"], replaces=replaces)


def usage(n):
    return dict(input_tokens=n, cached_input_tokens=0, output_tokens=n, total_tokens=2*n)


def result(ident="a", state="completed", n=10):
    return dict(id=ident, role="implement", status=state, summary="observed", usage=usage(n),
                execution=dict(termination="exited", exit_code=0), runtime_errors=[],
                session_identity_ok=True, artifacts=ident, observed_changed_files=[], observed_commands=[])


def report(status="completed"):
    return dict(status=status, summary="done", changed_files=[], checks=[], issues=[])


def decision(tasks=None, followups=None, final=None):
    return dict(summary="decision", tasks=tasks or [], followups=followups or [], report=final)


class FakeRuntime:
    report_schema = {"type": "object"}
    resumable = staticmethod(d.Runtime.resumable)

    def __init__(self, decisions):
        self.decisions = iter(decisions)
        self.calls, self.sessions, self.prompts = [], [], []
        self.turn = 0
        self.results, self.fixed = {}, {}
        self.violations = []
        self.invalid_scopes = False
        self.bad_resume = set()
        self.bad_frame = False
        self.session_override = None

    def prepare(self, profiles, limits):
        self.calls.append("prepare")

    @staticmethod
    def write(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def lock(self):
        return nullcontext()

    def handoff(self, known, output):
        return json.dumps(known)

    def decide(self, prompt, schema, out, session, timeout):
        self.turn += 1
        self.sessions.append(session)
        self.prompts.append(prompt)
        return dict(valid=not self.bad_frame, session_id=self.session_override or "head-session",
                    usage=usage(100*self.turn), final=next(self.decisions))

    @staticmethod
    def valid_report(value):
        return isinstance(value, dict) and value.get("status") in ("completed", "blocked", "failed")

    def validate_tasks(self, tasks, output):
        self.calls.append("validate-tasks")
        if self.invalid_scopes:
            raise ValueError("scope escape")
        return tasks

    def dispatch(self, tasks, profiles, output, limits):
        self.calls.append(("dispatch", copy.deepcopy(tasks)))
        return dict(tasks=[copy.deepcopy(self.results.get(t["id"], result(t["id"]))) for t in tasks],
                    scope_violations=self.violations)

    def validate_resume(self, item):
        self.calls.append(("validate-resume", item["id"]))
        if item["id"] in self.bad_resume:
            raise ValueError("invalid resume environment")

    def resume(self, old, prompt, output, timeout):
        self.calls.append(("resume", old["id"]))
        return copy.deepcopy(self.fixed.get(old["id"], result(old["id"], n=3)))


class DirectorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.profiles = {"sol": profile(), "glm": profile("glm", "opencode")}

    def run_flow(self, decisions, *, setup=None, limits=d.Limits()):
        runtime = FakeRuntime(decisions)
        if setup:
            setup(runtime)
        output = Path(self.tmp.name) / "run"
        result_ = d.run_director("User acceptance", self.profiles, runtime, output, limits)
        self.assertEqual(json.loads((output / "summary.json").read_text())["status"], result_["status"])
        return result_, runtime

    def test_direct_route_no_workers(self):
        out, runtime = self.run_flow([decision(final=report())])
        self.assertEqual(out["status"], "completed")
        self.assertEqual(out["worker_starts"], 0)
        self.assertIsNone(out["worker_usage"])
        self.assertEqual(runtime.sessions, [None])

    def test_research_then_implementation_then_review(self):
        steps = [decision([task("investigate", "glm", "research")]),
                 decision([task("build", "glm")]), decision([task("audit", "sol", "review")]),
                 decision(final=report())]
        out, runtime = self.run_flow(steps)
        self.assertEqual(out["status"], "completed")
        self.assertEqual(out["stages"], 3)
        self.assertEqual(out["main_usage"], usage(400))  # last cumulative, not 100+200+300+400
        self.assertEqual(runtime.sessions, [None, "head-session", "head-session", "head-session"])
        self.assertIn('"investigate"', runtime.prompts[1])
        self.assertEqual(out["worker_usage_by_profile"]["glm"]["invocations"], 2)

    def test_mixed_profile_batch(self):
        out, _ = self.run_flow([decision([task("a", "sol"), task("b", "glm")]), decision(final=report())])
        self.assertEqual(out["status"], "completed")
        self.assertEqual(set(out["worker_usage_by_profile"]), {"sol", "glm"})

    def test_correction_usage_is_delta(self):
        out, runtime = self.run_flow([decision([task()]), decision(followups=[dict(id="a", prompt="fix")]), decision(final=report())])
        self.assertEqual(out["status"], "completed")
        self.assertEqual(out["repairs"], 1)
        self.assertEqual(out["worker_usage"]["total_tokens"], 26)
        self.assertEqual(out["main_usage"]["total_tokens"], 600)
        self.assertIn(("resume", "a"), runtime.calls)

    def test_second_correction_rejected(self):
        correction = decision(followups=[dict(id="a", prompt="fix")])
        out, runtime = self.run_flow([decision([task()]), correction, correction])
        self.assertEqual(out["status"], "failed")
        self.assertEqual(runtime.calls.count(("resume", "a")), 1)

    def test_role_change_on_continuation_rejected(self):
        out, runtime = self.run_flow([decision([task()]), decision(followups=[dict(id="a", prompt="fix", role="implement")])])
        self.assertEqual(out["status"], "failed")
        self.assertNotIn(("resume", "a"), runtime.calls)

    def test_unknown_profile_rejected_before_dispatch(self):
        out, runtime = self.run_flow([decision([task(name="invented")])])
        self.assertEqual(out["status"], "failed")
        self.assertEqual(runtime.calls, ["prepare"])

    def test_role_allowlist(self):
        self.profiles["glm"]["roles"] = ["research"]
        out, _ = self.run_flow([decision([task(name="glm")])])
        self.assertEqual(out["status"], "failed")

    def test_scope_validation_before_dispatch(self):
        out, runtime = self.run_flow([decision([task()])], setup=lambda r: setattr(r, "invalid_scopes", True))
        self.assertEqual(out["status"], "failed")
        self.assertEqual(runtime.calls, ["prepare", "validate-tasks"])

    def test_duplicate_ids_no_dispatch(self):
        out, _ = self.run_flow([decision([task(), task()])])
        self.assertEqual(out["status"], "failed")

    def test_conflicting_actions(self):
        out, _ = self.run_flow([decision([task()], final=report())])
        self.assertEqual(out["status"], "failed")

    def test_empty_action(self):
        out, _ = self.run_flow([decision()])
        self.assertEqual(out["status"], "failed")

    def test_stage_limit(self):
        out, runtime = self.run_flow([decision([task()]), decision([task("b")])], limits=d.Limits(stages=1))
        self.assertEqual(out["status"], "failed")
        self.assertEqual(out["worker_starts"], 1)
        self.assertEqual(sum(isinstance(v, tuple) and v[0] == "dispatch" for v in runtime.calls), 1)

    def test_worker_limit_before_dispatch(self):
        out, _ = self.run_flow([decision([task(), task("b")])], limits=d.Limits(workers=1))
        self.assertEqual(out["worker_starts"], 0)
        self.assertEqual(out["status"], "failed")

    def test_runtime_failure_stops_without_fallback(self):
        out, runtime = self.run_flow([decision([task()])], setup=lambda r: r.results.update(a=result(state="failed")))
        self.assertEqual(out["status"], "failed")
        self.assertEqual(runtime.turn, 1)
        self.assertEqual(out["worker_usage"]["total_tokens"], 20)

    def test_unknown_usage_not_zero(self):
        unknown = result()
        unknown["usage"] = None
        out, _ = self.run_flow([decision([task()])], setup=lambda r: r.results.update(a=unknown))
        self.assertEqual(out["status"], "failed")
        self.assertIsNone(out["worker_usage"])

    def test_scope_violation_stops(self):
        out, runtime = self.run_flow([decision([task()])], setup=lambda r: setattr(r, "violations", ["outside.py"]))
        self.assertEqual(out["status"], "failed")
        self.assertEqual(runtime.turn, 1)

    def test_blocked_worker_cannot_be_silently_completed(self):
        out, _ = self.run_flow([decision([task()]), decision(final=report())],
                              setup=lambda r: r.results.update(a=result(state="blocked")))
        self.assertEqual(out["status"], "blocked")

    def test_explicit_escalation_preserves_contract(self):
        original = task(name="glm")
        replacement = dict(original, id="strong", profile="sol", replaces="a")
        out, _ = self.run_flow([decision([original]), decision([replacement]), decision(final=report())],
                              setup=lambda r: r.results.update(a=result(state="blocked")))
        self.assertEqual(out["status"], "completed")
        self.assertEqual(out["replaced"], ["a"])
        self.assertEqual(out["worker_starts"], 2)

    def test_escalation_cannot_weaken_acceptance(self):
        replacement = dict(task(), id="strong", replaces="a", acceptance=["Just exit"])
        out, _ = self.run_flow([decision([task()]), decision([replacement])],
                              setup=lambda r: r.results.update(a=result(state="blocked")))
        self.assertEqual(out["status"], "failed")
        self.assertEqual(out["worker_starts"], 1)

    def test_all_resume_preflights_before_any_correction(self):
        steps = [decision([task(), task("b")]), decision(followups=[dict(id="a", prompt="fix"), dict(id="b", prompt="fix")])]
        out, runtime = self.run_flow(steps, setup=lambda r: r.bad_resume.add("b"))
        self.assertEqual(out["status"], "failed")
        self.assertNotIn(("resume", "a"), runtime.calls)

    def test_head_failure_no_workers(self):
        out, runtime = self.run_flow([decision([task()])], setup=lambda r: setattr(r, "bad_frame", True))
        self.assertEqual(out["status"], "failed")
        self.assertEqual(runtime.calls, ["prepare"])

    def test_interrupted_dispatch_usage_is_unknown_not_omitted(self):
        runtime = FakeRuntime([decision([task()])])
        def interrupted(*args):
            raise KeyboardInterrupt()
        runtime.dispatch = interrupted
        out = d.run_director("request", self.profiles, runtime, Path(self.tmp.name) / "run")
        self.assertEqual(out["status"], "failed")
        self.assertEqual(out["worker_starts"], 1)
        self.assertIsNone(out["worker_usage"])
        self.assertIsNone(out["worker_usage_by_profile"]["sol"]["usage"])
        self.assertFalse(out["invocations"][0]["observed"])

    def test_failed_correction_does_not_report_initial_usage_as_total(self):
        runtime = FakeRuntime([decision([task()]), decision(followups=[dict(id="a", prompt="fix")])])
        def interrupted(*args):
            raise OSError("process state unknown")
        runtime.resume = interrupted
        out = d.run_director("request", self.profiles, runtime, Path(self.tmp.name) / "run")
        self.assertEqual(out["status"], "failed")
        self.assertIsNone(out["worker_usage"])
        self.assertEqual(len(out["invocations"]), 2)

    def test_failed_head_call_does_not_keep_stale_usage(self):
        runtime = FakeRuntime([decision([task()])])
        original = runtime.decide
        def fail_next(*args):
            if runtime.turn:
                raise OSError("unknown final head usage")
            return original(*args)
        runtime.decide = fail_next
        out = d.run_director("request", self.profiles, runtime, Path(self.tmp.name) / "run")
        self.assertEqual(out["status"], "failed")
        self.assertIsNone(out["main_usage"])

    def test_changed_head_session_rejected(self):
        runtime = FakeRuntime([decision([task()]), decision(final=report())])
        original = runtime.decide
        def decide(*args):
            frame = original(*args)
            if runtime.turn == 2:
                frame["session_id"] = "different"
            return frame
        runtime.decide = decide
        out = d.run_director("request", self.profiles, runtime, Path(self.tmp.name) / "run")
        self.assertEqual(out["status"], "failed")


class ProfileTests(unittest.TestCase):
    def check_invalid(self, patch_):
        row = profile()
        row.update(patch_)
        with self.assertRaises(ValueError):
            d.profiles_from(dict(version=1, profiles=[row]))

    def test_operator_example(self):
        path = Path(__file__).resolve().parents[1] / "examples/director-profiles.json"
        self.assertEqual(list(d.profiles_from(json.loads(path.read_text()))), ["sol"])

    def test_disabled_placeholder_not_dispatched(self):
        row = profile("glm", "opencode")
        row.update(enabled=False, model="provider/REPLACE_MODEL")
        self.assertEqual(list(d.profiles_from(dict(version=1, profiles=[profile(), row]))), ["sol"])

    def test_enabled_placeholder_rejected(self):
        self.check_invalid(dict(backend="opencode", model="provider/REPLACE_MODEL"))

    def test_invalid_profiles(self):
        for change in (dict(id="../x"), dict(model="a\n-b"), dict(effort='high"\nfoo=true'),
                       dict(enabled="true"), dict(backend="other"), dict(roles=["root"]),
                       dict(roles=[]), dict(backend="opencode", model="no-provider")):
            with self.subTest(change=change):
                self.check_invalid(change)

    def test_duplicate_id(self):
        with self.assertRaises(ValueError):
            d.profiles_from(dict(version=1, profiles=[profile(), profile()]))

    def test_empty_allowlist(self):
        with self.assertRaises(ValueError):
            d.profiles_from(dict(version=1, profiles=[dict(profile(), enabled=False)]))

    def test_limits(self):
        for args in (dict(stages=0), dict(concurrency=4), dict(workers=13), dict(workers=True),
                     dict(timeout=float("nan")), dict(timeout=float("inf")), dict(timeout=0)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                d.Limits(**args)

    def test_unknown_usage_propagates(self):
        self.assertIsNone(d.sum_usage([dict(usage=usage(1)), dict(usage=None)]))
        self.assertIsNone(d.sum_usage([]))


class SchedulerTests(unittest.TestCase):
    """Exercise the real scheduler with fake existing execution primitives."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.calls, self.active, self.peak = [], 0, 0
        self.guard = threading.Lock()
        self.fail_init = False
        self.conflicting = False
        self.fail_id = None
        self.start_barrier = None
        backend = SimpleNamespace(validate_backend=lambda *a: None,
                                  resolve_cli=lambda c: c, prepare_env=lambda env, role: env)
        def execute(t, out, cli, model, effort, timeout, cancel, **kw):
            with self.guard:
                self.active += 1
                self.peak = max(self.peak, self.active)
                self.calls.append(("start", t["id"], model, kw["backend"], t["role"]))
            try:
                if self.start_barrier:
                    self.start_barrier.wait(timeout=5)
                time.sleep(0.01)
                if self.fail_id == t["id"]:
                    raise OSError("launch failed")
                return result(t["id"])
            finally:
                with self.guard:
                    self.active -= 1
                    self.calls.append(("end", t["id"]))
        def run_process(*args, **kwargs):
            self.calls.append(("init",))
            return dict(termination="exited", exit_code=1 if self.fail_init else 0)
        fake = SimpleNamespace(git_root=lambda cwd: self.root, REPORT_SCHEMA={},
                               write_json=FakeRuntime.write, validate_report=FakeRuntime.valid_report,
                               opencode_backend=backend, snapshot=lambda *a: {},
                               scope_violations=lambda *a: [], execute_task=execute,
                               conflict=lambda a, b: self.conflicting, run_process=run_process)
        with patch.dict(sys.modules, {"worker": fake, "team": SimpleNamespace()}):
            self.runtime = d.Runtime(self.root, main_model="main")
        self.profiles = {"sol": profile(), "glm": profile("glm", "opencode"), "ds": profile("ds", "opencode")}
        self.profiles["ds"]["model"] = "provider/different"

    def dispatch(self, tasks, concurrency=3):
        return self.runtime.dispatch(tasks, self.profiles, self.root / "stage", d.Limits(concurrency=concurrency))

    def test_mixed_models_execute_in_parallel(self):
        self.start_barrier = threading.Barrier(3)
        out = self.dispatch([task("a", "glm"), task("b", "ds"), task("c", "sol", "review")])
        self.assertEqual(self.peak, 3)
        self.assertEqual(self.calls[0], ("init",))
        starts = [row for row in self.calls if row[0] == "start"]
        self.assertEqual({row[2] for row in starts}, {"model", "provider/model", "provider/different"})
        self.assertEqual([r["id"] for r in out["tasks"]], ["a", "b", "c"])
        self.assertEqual(sum(row == ("init",) for row in self.calls), 1)

    def test_concurrency_one(self):
        self.dispatch([task("a"), task("b", "glm")], concurrency=1)
        self.assertEqual(self.peak, 1)

    def test_overlapping_scopes_keep_order(self):
        self.conflicting = True
        self.dispatch([task("a"), task("b", "glm")])
        self.assertEqual(self.peak, 1)
        self.assertLess(self.calls.index(("end", "a")), next(i for i, v in enumerate(self.calls) if v[:2] == ("start", "b")))

    def test_failed_initialization_calls_no_workers(self):
        self.fail_init = True
        with self.assertRaises(ValueError):
            self.dispatch([task("a", "glm"), task("b", "ds")])
        self.assertEqual(self.calls, [("init",)])

    def test_exception_retains_other_results(self):
        self.fail_id = "a"
        out = self.dispatch([task("a"), task("b", "glm")])
        self.assertEqual([r["status"] for r in out["tasks"]], ["failed", "completed"])
        self.assertIsNone(out["tasks"][0]["usage"])


if __name__ == "__main__":
    unittest.main()
