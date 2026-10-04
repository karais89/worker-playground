"""Paired CLI benchmark. Host diagnostics never count as isolated benchmark evidence."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import time
import uuid

import worker
import team

ROOT = Path(__file__).resolve().parent
TEST_INSTRUCTION = " Add standard-library unittest tests discoverable with python -m unittest discover -s . and make them pass."
CASES = {
    "bug": {
        "files": {"flags.py": "def parse_flag(value):\n    return bool(value)\n"},
        "prompt": "Fix flags.parse_flag(value). Accept booleans unchanged and case-insensitive strings true/false with surrounding whitespace. Reject other values with ValueError, including integers. Add appropriate tests.",
        "grade": """
from flags import parse_flag
assert parse_flag(True) is True
assert parse_flag(False) is False
assert parse_flag(' FALSE ') is False
assert parse_flag('TrUe') is True
for value in [0, 1, None, '', 'yes', [], {}]:
    try: parse_flag(value)
    except ValueError: pass
    else: raise AssertionError(repr(value))
""",
    },
    "modules": {
        "files": {"slug.py": "def slugify(text):\n    raise NotImplementedError\n",
                  "stats.py": "def summarize(values):\n    raise NotImplementedError\n"},
        "prompt": "Implement two independent modules and add tests. slug.slugify(text) lowercases ASCII letters, treats every run of non-ASCII-alphanumeric characters as one hyphen, and strips boundary hyphens. stats.summarize(values) returns a dict with count, min, max and mean; for empty values count is 0 and the other three values are None. Support negative numbers and floats. Preserve input lists.",
        "grade": """
from slug import slugify
from stats import summarize
assert slugify(' Hello, WORLD! ') == 'hello-world'
assert slugify('a___b  c') == 'a-b-c'
assert slugify('---') == ''
assert slugify('Café 42') == 'caf-42'
assert summarize([]) == dict(count=0,min=None,max=None,mean=None)
data = [-2, 1, 4.5]
assert summarize(data) == dict(count=3,min=-2,max=4.5,mean=3.5/3)
assert data == [-2, 1, 4.5]
""",
    },
    "investigate": {
        "files": {
            "config.py": "DEFAULTS = {'server': {'host': 'localhost', 'port': 80, 'tls': {'enabled': False, 'options': {'versions': ['1.2'], 'verify': True}}}, 'debug': False}\n",
            "loader.py": "from config import DEFAULTS\n\ndef load_config(overrides):\n    result = DEFAULTS.copy()\n    result.update(overrides)\n    return result\n",
        },
        "prompt": "Investigate why load_config loses nested defaults. Fix loader.load_config(overrides) to recursively merge dictionaries: overriding one nested key preserves others; non-dictionaries replace old values. Returned mutable structures must not alias DEFAULTS or overrides. Add regression tests.",
        "grade": """
from config import DEFAULTS
from loader import load_config
overrides = {'server': {'port': 443}, 'extra': {'items': [1,2]}}
a = load_config(overrides)
assert a['server']['host'] == 'localhost' and a['server']['port'] == 443
assert a['debug'] is False
a['server']['host'] = 'changed'
a['extra']['items'].append(3)
assert DEFAULTS['server']['host'] == 'localhost'
assert overrides['extra']['items'] == [1,2]
assert load_config({'server': None})['server'] is None
assert load_config({})['server']['port'] == 80
nested = {'server': {'tls': {'options': {'versions': ['1.3']}}}}
b = load_config(nested)
assert b['server']['tls']['enabled'] is False
assert b['server']['tls']['options'] == {'versions': ['1.3'], 'verify': True}
b['server']['tls']['options']['versions'].append('changed')
assert nested['server']['tls']['options']['versions'] == ['1.3']
assert DEFAULTS['server']['tls']['options']['versions'] == ['1.2']
c = load_config({})
c['server']['tls']['options']['versions'].append('changed')
assert DEFAULTS['server']['tls']['options']['versions'] == ['1.2']
assert load_config({'server': {'tls': {'options': None}}})['server']['tls']['options'] is None
""",
    },
}


def prepare_case(repo, case):
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=repo, check=True)
    for name, content in case["files"].items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(content, encoding="utf-8")
    (repo / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=Benchmark", "-c", "user.email=benchmark@localhost",
                    "-c", "commit.gpgsign=false", "commit", "-qm", "fixed fixture"], cwd=repo, check=True)


def clean_environment(home: Path, auth_source: Path):
    """A fresh user profile. OS isolation must additionally be supplied by the operator."""
    home.mkdir(parents=True)
    codex_home = home / ".codex"
    codex_home.mkdir()
    if not auth_source.is_file():
        raise ValueError("ChatGPT CLI auth file not found; supply --auth-file from a signed-in CLI")
    shutil.copyfile(auth_source, codex_home / "auth.json")
    if os.name != "nt":
        (codex_home / "auth.json").chmod(0o600)
    (codex_home / "config.toml").write_text('''approval_policy = "never"
sandbox_mode = "workspace-write"
[agents]
enabled = false
[features]
apps = false
plugins = false
hooks = false
skip_host_skill_discovery = true
[memories]
generate_memories = false
use_memories = false
[windows]
sandbox = "unelevated"
''', encoding="utf-8")
    keep = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "PROCESSOR_ARCHITECTURE",
            "NUMBER_OF_PROCESSORS", "SYSTEMDRIVE", "PROGRAMDATA", "ALLUSERSPROFILE", "PROGRAMFILES",
            "PROGRAMFILES(X86)", "PROGRAMW6432", "PUBLIC", "OS",
            "SSL_CERT_FILE", "SSL_CERT_DIR", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY"}
    env = {k: v for k, v in os.environ.items() if k.upper() in keep}
    env.update(HOME=str(home), USERPROFILE=str(home), CODEX_HOME=str(codex_home),
               XDG_CONFIG_HOME=str(home / "config"), XDG_DATA_HOME=str(home / "data"),
               XDG_CACHE_HOME=str(home / "cache"), APPDATA=str(home / "appdata"),
               LOCALAPPDATA=str(home / "localappdata"), TEMP=str(home / "tmp"), TMP=str(home / "tmp"),
               PYTHONUTF8="1", PYTHONIOENCODING="utf-8", GIT_CONFIG_NOSYSTEM="1",
               GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0")
    for name in ("config", "data", "cache", "appdata", "localappdata", "tmp"):
        (home / name).mkdir()
    return env


@contextmanager
def runtime_profile(output):
    # Nested CLIs run inside the parent's sandbox and need access to their shared profile.
    # A normal directory inherits Windows sandbox-compatible ACLs. It is removed on exit.
    home = output / (".runtime-profile-" + uuid.uuid4().hex)
    try:
        yield home
    finally:
        if home.exists():
            if home.is_symlink() or home.resolve().parent != output.resolve():
                raise ValueError("refusing to remove an unexpected profile path")
            shutil.rmtree(home)


def grade(repo, case):
    # Grader code is never written to the agent's repository or prompt.
    prefix = "import sys\nsys.path.insert(0, " + repr(str(repo)) + ")\n"
    def check(program):
        try:
            result = subprocess.run([sys.executable, "-I", "-c", prefix + program], cwd=repo,
                                    capture_output=True, timeout=30)
            return dict(passed=result.returncode == 0, exit_code=result.returncode,
                        stdout=result.stdout.decode("utf-8", "replace"), stderr=result.stderr.decode("utf-8", "replace"))
        except subprocess.TimeoutExpired:
            return dict(passed=False, exit_code=None, stderr="grader timeout")
    functional = check(case["grade"])
    generated = check('''import unittest
suite = unittest.defaultTestLoader.discover('.', pattern='test*.py')
result = unittest.TextTestRunner(verbosity=2).run(suite)
sys.exit(0 if result.wasSuccessful() and result.testsRun > len(result.skipped) else 1)
''')
    return dict(passed=functional["passed"] and generated["passed"], functional=functional, generated_tests=generated)


def main_prompt(case):
    common = ("Complete this coding task. Do not commit or change git configuration. "
              "Run relevant tests. Do not use native subagents. Stay inside the project and supplied artifact directories. "
              "Return the final report using the supplied schema.\nTask: " + case["prompt"] + TEST_INSTRUCTION + "\n")
    return common + "Work directly without delegating or launching another AI CLI."


def run_one(case_name, arm, output, args):
    case = CASES[case_name]
    output.mkdir(parents=True, exist_ok=False)
    with runtime_profile(output) as home:
        repo = output / "project"
        prepare_case(repo, case)
        env = clean_environment(home, args.auth_file)
        cli = shutil.which(args.codex) or args.codex
        config_hash = hashlib.sha256((home / ".codex/config.toml").read_bytes()).hexdigest()
        before = worker.snapshot(repo, ["."])
        if arm == "team":
            started = time.monotonic()
            coordination = team.run_team(case["prompt"] + TEST_INSTRUCTION, repo, output / "coordination", [cli],
                                         args.main_model, args.worker_model, args.effort,
                                         args.worker_effort, args.timeout, env)
            valid = coordination["main_valid"] and coordination["status"] == "completed"
            main_usage = coordination["main_usage"]
            execution = dict(exit_code=0 if valid else 1, termination="exited",
                             seconds=round(time.monotonic() - started, 3))
        else:
            worker.write_json(output / "schema.json", worker.REPORT_SCHEMA)
            command = [cli, "--no-daemon", "-C", str(repo), "exec", "--json", "--color", "never", "-m", args.main_model,
                       "-c", f'model_reasoning_effort="{args.effort}"', "--add-dir", str(output),
                       "--output-schema", str(output / "schema.json"),
                       "--output-last-message", str(output / "final.json"), "-"]
            prompt = main_prompt(case)
            (output / "prompt.txt").write_text(prompt, encoding="utf-8")
            worker.write_json(output / "command.json", command)
            execution = worker.run_process(command, str(repo), prompt, output, args.timeout, env=env)
            events = worker.parse_events(output / "events.jsonl")
            main_usage = events["usage"]
            try:
                final = worker.read_json(output / "final.json")
            except (ValueError, OSError):
                final = None
            valid = (execution["exit_code"] == 0 and execution["termination"] == "exited"
                     and events["completed_turns"] == 1 and not events["errors"]
                     and worker.validate_report(final) and final["status"] == "completed")
        valid = valid and main_usage is not None
        grading = grade(repo, case)
        worker.write_json(output / "grade.json", grading)
        changed = worker.save_diff(before, worker.snapshot(repo, ["."]), output)
        reports = [worker.read_json(p) for p in output.rglob("report.json") if p != output / "report.json"]
        model_contexts = []
        # Available on CLI versions that persist rollouts. Unknown is kept explicit.
        for rollout in (home / ".codex/sessions").rglob("*.jsonl"):
            for line in rollout.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if item.get("type") == "turn_context":
                    payload = item.get("payload", {})
                    model_contexts.append({"model": payload.get("model"), "effort": payload.get("effort")})
        batch_failures = sum(worker.read_json(p).get("status") != "completed"
                             for p in output.rglob("summary.json"))
        result = dict(case=case_name, arm=arm, valid=valid, passed=grading["passed"],
                      environment="host-diagnostic" if args.diagnostic else args.isolated_environment,
                      isolation_basis="fresh-profile-only" if args.diagnostic else "operator-attested-VM-or-container",
                      profile_config_sha256=config_hash, requested_main_model=args.main_model,
                      requested_worker_model=args.worker_model, requested_effort=args.effort,
                      requested_worker_effort=args.worker_effort, observed_model_contexts=model_contexts,
                      main_usage=main_usage, worker_usage=worker.total_usage(reports) if reports else {
                          k: 0 for k in ("input_tokens", "cached_input_tokens", "output_tokens", "total_tokens")},
                      worker_invocations=len(reports), worker_failures=sum(r["status"] != "completed" for r in reports),
                      worker_batch_failures=batch_failures,
                      actual_delegation=bool(reports), changed_files=changed, execution=execution,
                      artifacts=str(output))
        worker.write_json(output / "result.json", result)
        return result


def eligible(row):
    return (row["valid"] and row["passed"] and row["main_usage"] is not None
            and not row["worker_failures"] and not row.get("worker_batch_failures", 0)
            and (not row["actual_delegation"] or row["worker_usage"] is not None))


def summarize(results, diagnostic):
    comparisons = []
    for name in sorted({r["case"] for r in results}):
        rows = [r for r in results if r["case"] == name]
        solo = [r for r in rows if r["arm"] == "solo"]
        team = [r for r in rows if r["arm"] == "team"]
        measurable = bool(solo and team) and len(solo) == len(team) and all(eligible(r) for r in rows)
        delegated = bool(team) and all(r["actual_delegation"] for r in team)
        a = statistics.median(r["main_usage"]["total_tokens"] for r in solo) if measurable else None
        b = statistics.median(r["main_usage"]["total_tokens"] for r in team) if measurable else None
        comparisons.append(dict(case=name, solo_runs=len(solo), team_runs=len(team),
                                all_passed=all(r["passed"] for r in rows), delegation_observed=delegated,
                                delegated_team_runs=sum(bool(r["actual_delegation"]) for r in team),
                                solo_main_median=a, team_main_median=b,
                                diagnostic_reduction=1-b/a if measurable and a else None,
                                isolated_main_reduction=1-b/a if measurable and a and not diagnostic else None))
    return dict(diagnostic=diagnostic, runs=results, comparisons=comparisons,
                note="Input includes cached input; output already includes reasoning. No extra addition. Repeated-run estimates are exploratory, not statistical proof.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=[*CASES, "all"], default="bug")
    parser.add_argument("--arms", nargs="+", choices=["solo", "team"], default=["solo", "team"])
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--main-model", default="gpt-6-astra")
    parser.add_argument("--worker-model", default=worker.DEFAULT_MODEL)
    parser.add_argument("--effort", default="high")
    parser.add_argument("--worker-effort", default="high")
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--auth-file", type=Path,
                        default=Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "auth.json")
    parser.add_argument("--output", type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--diagnostic", action="store_true", help="fresh profiles on current host; no isolated savings claim")
    mode.add_argument("--isolated-environment", help="operator attestation: identify the fresh VM/container image")
    args = parser.parse_args(argv)
    if args.repeats < 1 or args.timeout <= 0 or len(set(args.arms)) != len(args.arms):
        parser.error("positive repeats/timeout and distinct arms are required")
    output = (args.output or Path("bench-runs") / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])).resolve()
    try:
        output.mkdir(parents=True, exist_ok=False)
        version = subprocess.run([args.codex, "--version"], capture_output=True, text=True, check=True).stdout.strip()
        worker.write_json(output / "manifest.json", dict(cli_version=version, python=sys.version, platform=sys.platform,
                                                        main_model=args.main_model, worker_model=args.worker_model,
                                                        diagnostic=args.diagnostic,
                                                        fixture_sha256=hashlib.sha256(json.dumps(CASES, sort_keys=True).encode()).hexdigest(),
                                                        runner_sha256=hashlib.sha256((ROOT / "worker.py").read_bytes()).hexdigest(),
                                                        coordinator_sha256=hashlib.sha256((ROOT / "team.py").read_bytes()).hexdigest(),
                                                        benchmark_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()))
        results = []
        for case in CASES if args.case == "all" else [args.case]:
            for repeat in range(args.repeats):
                for arm in args.arms if repeat % 2 == 0 else list(reversed(args.arms)):
                    print(f"Running {case} {arm} repeat {repeat+1}", file=sys.stderr, flush=True)
                    result = run_one(case, arm, output / f"{case}-{repeat+1}-{arm}", args)
                    results.append(result)
                    worker.write_json(output / "summary.json", summarize(results, args.diagnostic))
        print(json.dumps(summarize(results, args.diagnostic)["comparisons"], indent=2))
        print(f"Artifacts: {output}")
        return 0 if all(eligible(r) for r in results) else 1
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(f"bench: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
