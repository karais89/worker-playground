"""Live regression of evidence-based review, reusing the original six cases.

Two additional controls: an unfixed pagination variant must remain blocked;
correct CSV with explicit CR round-trip evidence must not be rejected.
This is a controlled review experiment, not a solo/team quality benchmark.
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
import worker
from benchmarks import review_quality as quality

SCHEDULE = [
    ("atomic_import", True, "original"),
    ("paged_listing", False, "original"),
    ("independent_utilities", True, "original"),
    ("atomic_import", False, "original"),
    ("paged_listing", True, "original"),
    ("independent_utilities", False, "original"),
    ("paged_listing", True, "unfixed_variant"),
    ("independent_utilities", False, "verified_csv"),
]

UTILITY_CHECK = quality.source('''
    import csv, io, json, tempfile
    from pathlib import Path
    from taskdesk.domain import Task
    from taskdesk.reporting import to_csv
    from taskdesk.settings import load_settings
    for title in ['a'+chr(13)+'b', 'a'+chr(13)+chr(10)+'b', 'x,"y"']:
        rows = list(csv.reader(io.StringIO(to_csv([Task(1,title,'open',2)]), newline='')))
        assert rows[1] == ['1',title,'open','2'], rows
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp)/'config.json'
        path.write_text(json.dumps({'report':None}))
        assert load_settings(path, env={'TASKDESK_PAGE_SIZE':'17'})['report']['page_size'] == 17
    print('CSV CR/CRLF/quote round-trip and null-report override passed')
''')


def stage_variant(name, repo, faulty, variant):
    quality.stage(name, repo, faulty)
    if variant == "unfixed_variant":
        path = repo/"taskdesk/storage.py"
        text = path.read_text()
        assert 'limit, 0])' in text
        path.write_text(text.replace('limit, 0])', 'limit, offset + (1 if offset else 0)])'))
    elif variant == "verified_csv":
        path = repo/"taskdesk/settings.py"
        text = path.read_text()
        before = "        result['report']['page_size'] = size"
        assert before in text
        path.write_text(text.replace(before, "        if not isinstance(result.get('report'), dict):\n            result['report'] = {}\n" + before))
        path = repo/"test_csv_export.py"
        path.write_text(path.read_text() + quality.source('''
            class CsvBoundaryTests(unittest.TestCase):
                def test_bare_cr_and_crlf_round_trip(self):
                    for title in ['a'+chr(13)+'b', 'a'+chr(13)+chr(10)+'b']:
                        rows = list(csv.reader(io.StringIO(to_csv([Task(1,title,'open',2)]), newline='')))
                        self.assertEqual(rows[1], ['1',title,'open','2'])
        '''))


def utility_check(repo):
    result = subprocess.run([sys.executable, '-I', '-c',
        'import sys\nsys.path.insert(0, ' + repr(str(repo)) + ')\n' + UTILITY_CHECK],
        cwd=repo, capture_output=True, text=True, timeout=30)
    return dict(passed=result.returncode == 0, exit_code=result.returncode,
                stdout=result.stdout, stderr=result.stderr)


def controls():
    rows = quality.controls()
    for name, faulty, variant in SCHEDULE[-2:]:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)/'project'
            case = quality.cases()[name]
            bench.prepare_case(repo, case)
            stage_variant(name, repo, faulty, variant)
            grade = bench.grade(repo, case)
            assert grade['generated_tests']['passed'], grade
            assert grade['functional']['passed'] == (not faulty), grade
            if variant == 'verified_csv':
                assert utility_check(repo)['passed']
            rows.append(dict(case=name, variant=variant, smoke_passed=True,
                             external_passed=grade['functional']['passed']))
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--auth-file', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--controls-only', action='store_true')
    parser.add_argument('--start-case', type=int, choices=range(1, 9), default=1,
                        help='Start a fresh run of this case and the remaining cases; never resume model sessions')
    args = parser.parse_args()
    validation = controls()
    if args.controls_only:
        print(json.dumps(validation, indent=2))
        return 0
    if args.auth_file is None or args.output is None:
        parser.error('--auth-file and --output are required for live execution')
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    names = ['team.py', 'worker.py', 'bench.py', 'benchmarks/review_quality.py',
             'benchmarks/review_evidence.py', 'benchmarks/project_cases.py', 'benchmarks/validate_project_cases.py']
    schedule = SCHEDULE[args.start_case - 1:]
    worker.write_json(args.output/'manifest.json', dict(schedule=schedule, start_case=args.start_case, controls=validation,
        source_commit=subprocess.check_output(['git','rev-parse','HEAD'], cwd=ROOT, text=True).strip(),
        source_sha256={p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in names},
        model='gpt-6.1-sol', effort='high', python=sys.version,
        cli=subprocess.check_output(['codex','--version'], text=True).strip(),
        method='Six original staged cases, one withheld unfixed variant, one verified CSV control; fresh sessions; no solo comparison'))
    original_stage, original_resume = quality.stage, worker.resume_task
    rows, stopped = [], None
    for index, (name, faulty, variant) in enumerate(schedule, args.start_case):
        print(f'Running q{index:02d} {name} {variant}', flush=True)
        def stage(name, repo, faulty):
            # stage_variant calls the unpatched original to avoid recursion.
            with patch.object(quality, 'stage', original_stage):
                stage_variant(name, repo, faulty, variant)
        def resume(prior, output, prompt, *a, **kw):
            if variant == 'unfixed_variant':
                prompt = ('Controlled check-only follow-up: do not edit any files. Inspect and reproduce '
                          'the requested issues, report actual results and leave unresolved defects blocked.\n' + prompt)
            return original_resume(prior, output, prompt, *a, **kw)
        with patch.object(quality, 'stage', side_effect=stage), patch.object(worker, 'resume_task', side_effect=resume):
            row = quality.run_case(name, faulty, args.output/f'q{index:02d}', args)
        row['variant'] = variant
        row['extra_checks'] = utility_check(args.output/f'q{index:02d}'/'project') if name == 'independent_utilities' else None
        expected_block = variant == 'unfixed_variant'
        row['acceptance_passed'] = (not row['initial_worker_modified_code']
            and row['coordinator']['main_valid'] and row['coordinator']['main_usage'] is not None
            and row['coordinator']['worker_usage'] is not None
            and row['coordinator']['status'] == ('blocked' if expected_block else 'completed')
            and row['after_grade']['passed'] == (not expected_block)
            and (row['extra_checks'] is None or row['extra_checks']['passed']))
        rows.append(row)
        worker.write_json(args.output/f'q{index:02d}'/'result.json', row)
        for path in (args.output/f'q{index:02d}').rglob('events.jsonl'):
            if any('out of credits' in e.lower() or 'usage limit' in e.lower() for e in worker.parse_events(path)['errors']):
                stopped = 'CLI credits or usage limit exhausted'
        worker.write_json(args.output/'summary.json', dict(runs=rows, stopped_reason=stopped, remaining_schedule=schedule[len(rows):]))
        print(json.dumps(dict(case=name, variant=variant, status=row['coordinator']['status'], acceptance_passed=row['acceptance_passed'])), flush=True)
        if stopped:
            break
    return 0 if len(rows) == len(schedule) and all(r['acceptance_passed'] for r in rows) else 1


if __name__ == '__main__':
    raise SystemExit(main())
