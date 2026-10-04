"""Model-free positive and negative controls for the simulated project grader."""
from pathlib import Path
import sys
import tempfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bench
from benchmarks.project_cases import FILES, cases, source


def reference(name, repo):
    def replace(path, before, after):
        file = repo / path
        text = file.read_text(encoding="utf-8")
        assert before in text, (name, path, before)
        file.write_text(text.replace(before, after), encoding="utf-8")
    if name == "atomic_import":
        replace("taskdesk/service.py", '        for record in records:\n            self.store.add(*validate_record(record))',
                '        with self.store.connection:\n            for record in records:\n                self.store.connection.execute("INSERT INTO tasks(title,status,priority) VALUES (?,?,?)", validate_record(record))')
    elif name == "paged_listing":
        replace("taskdesk/storage.py", "def list_tasks(self, status=None):", "def list_tasks(self, status=None, *, sort='id', limit=None, offset=0):")
        replace("taskdesk/storage.py", '        query += " ORDER BY id ASC"',
                '        if sort not in ("id", "priority") or (limit is not None and limit < 0) or offset < 0:\n            raise ValueError("invalid listing options")\n        query += " ORDER BY " + ("id ASC" if sort == "id" else "priority DESC, id ASC")\n        query += " LIMIT ? OFFSET ?"\n        values.extend([-1 if limit is None else limit, offset])')
        replace("taskdesk/service.py", "def list_tasks(self, status=None):", "def list_tasks(self, status=None, *, sort='id', limit=None, offset=0):")
        replace("taskdesk/service.py", "return self.store.list_tasks(status)", "return self.store.list_tasks(status, sort=sort, limit=limit, offset=offset)")
        replace("taskdesk/cli.py", '    importing = commands.add_parser("import")',
                '    listing.add_argument("--sort", choices=("id", "priority"), default="id")\n    listing.add_argument("--limit", type=int)\n    listing.add_argument("--offset", type=int, default=0)\n    importing = commands.add_parser("import")')
        replace("taskdesk/cli.py", "service.list_tasks(args.status)", "service.list_tasks(args.status, sort=args.sort, limit=args.limit, offset=args.offset)")
    else:
        file = repo / "taskdesk/reporting.py"
        file.write_text(file.read_text() + source('''
            import csv, io
            def to_csv(tasks):
                output = io.StringIO(newline='')
                writer = csv.writer(output, lineterminator='\\n')
                writer.writerow(['id','title','status','priority'])
                for task in tasks:
                    writer.writerow([task.id,task.title,task.status,task.priority])
                return output.getvalue()
        '''), encoding="utf-8")
        file = repo / "taskdesk/settings.py"
        prefix = FILES["taskdesk/settings.py"].split("def load_settings")[0]
        file.write_text(prefix + source('''
            import os
            def merge(old, new):
                if not isinstance(old, dict) or not isinstance(new, dict):
                    return deepcopy(new)
                result = deepcopy(old)
                for key, value in new.items():
                    result[key] = merge(result.get(key), value)
                return result

            def load_settings(path=None, env=None):
                env = os.environ if env is None else env
                value = {} if path is None else json.loads(Path(path).read_text(encoding='utf-8'))
                if not isinstance(value, dict):
                    raise ValueError('root must be an object')
                result = merge(DEFAULTS, value)
                if 'TASKDESK_PAGE_SIZE' in env:
                    size = int(env['TASKDESK_PAGE_SIZE'])
                    if not 1 <= size <= 1000:
                        raise ValueError('invalid page size')
                    result['report']['page_size'] = size
                return result
        '''), encoding="utf-8")


def validate():
    evidence = []
    for name, case in cases().items():
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "project"
            bench.prepare_case(repo, case)
            # Neutral test modules satisfy the file/count contract so rejection
            # must come from external behavioral checks, not a missing test file.
            for path in ("test_atomic_import.py", "test_listing.py", "test_csv_export.py", "test_settings.py"):
                (repo/path).write_text("import unittest\nclass Control(unittest.TestCase):\n    def test_control(self):\n        self.assertTrue(True)\n")
            initial = bench.grade(repo, case)
            assert not initial["functional"]["passed"] and initial["generated_tests"]["passed"], initial
            reference(name, repo)
            good = bench.grade(repo, case)
            assert good["passed"], (name, good)
            if name == "atomic_import":
                path = repo/"taskdesk/service.py"
                text = path.read_text().replace('validate_record(record))', 'validate_record(record))\n                self.store.connection.commit()')
            elif name == "paged_listing":
                path = repo/"taskdesk/storage.py"
                text = path.read_text().replace('limit, offset])', 'limit, 0])')
            else:
                path = repo/"taskdesk/settings.py"
                text = path.read_text().replace('os.environ if env is None else env', 'env or os.environ')
            path.write_text(text, encoding="utf-8")
            bad = bench.grade(repo, case)
            assert not bad["functional"]["passed"] and bad["generated_tests"]["passed"], (name,bad)
            evidence.append(dict(case=name, unchanged_rejected=True, reference_passed=True, mutant_rejected=True))
    return evidence


if __name__ == "__main__":
    import json
    print(json.dumps(validate(), indent=2))
