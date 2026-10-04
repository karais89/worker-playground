"""Synthetic multi-file application and fixed external acceptance checks.

This is a simulation, not an existing OSS project or a public benchmark score.
Reference changes are used only to validate the grader before model execution.
"""
from pathlib import Path
import textwrap


def source(text):
    return textwrap.dedent(text).lstrip()


FILES = {
    "README.md": source('''
        # TaskDesk
        A standard-library Python task tracker with SQLite persistence, JSON
        imports, reusable reporting helpers, and a command-line interface.

        Run `python -m taskdesk --db tasks.db add "Write report" --priority 2`.
        Run `python -m taskdesk --db tasks.db list --status open` for JSON output.
        Run `python -m taskdesk --db tasks.db import tasks.json` for a JSON list
        of objects containing title and optional status/priority fields.
        Import returns {"imported": N}; invalid input exits 2 with stderr text.
        Status values are open and done; priority is an integer from 0 to 5.

        Layout: domain.py validates data; storage.py owns SQLite operations;
        service.py implements application use cases; cli.py handles arguments.
        reporting.py and settings.py are separate public library utilities.
        Keep public APIs compatible unless the requested feature extends them.
        Run all tests with `python -m unittest discover -s .`.
    '''),
    "taskdesk/__init__.py": '"""TaskDesk application package."""\n',
    "taskdesk/__main__.py": "from .cli import main\nraise SystemExit(main())\n",
    "taskdesk/domain.py": source('''
        from dataclasses import dataclass, asdict

        @dataclass(frozen=True)
        class Task:
            id: int
            title: str
            status: str
            priority: int

            def to_dict(self):
                return asdict(self)

        def validate_record(record):
            if not isinstance(record, dict):
                raise ValueError("task must be an object")
            title = record.get("title")
            status = record.get("status", "open")
            priority = record.get("priority", 0)
            if not isinstance(title, str) or not title.strip():
                raise ValueError("title must be a nonempty string")
            if status not in ("open", "done"):
                raise ValueError("invalid status")
            if type(priority) is not int or not 0 <= priority <= 5:
                raise ValueError("priority must be an integer from 0 to 5")
            return title.strip(), status, priority
    '''),
    "taskdesk/storage.py": source('''
        import sqlite3
        from .domain import Task

        class Store:
            def __init__(self, path):
                self.connection = sqlite3.connect(path)
                self.connection.row_factory = sqlite3.Row
                self.connection.execute("CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL UNIQUE, status TEXT NOT NULL, priority INTEGER NOT NULL)")
                self.connection.commit()

            def close(self):
                self.connection.close()

            def add(self, title, status="open", priority=0):
                with self.connection:
                    cursor = self.connection.execute("INSERT INTO tasks(title,status,priority) VALUES (?,?,?)", (title,status,priority))
                return Task(cursor.lastrowid, title, status, priority)

            def list_tasks(self, status=None):
                query = "SELECT id,title,status,priority FROM tasks"
                values = []
                if status is not None:
                    query += " WHERE status=?"
                    values.append(status)
                query += " ORDER BY id ASC"
                return [Task(**dict(row)) for row in self.connection.execute(query, values)]

            def set_status(self, ident, status):
                with self.connection:
                    cursor = self.connection.execute("UPDATE tasks SET status=? WHERE id=?", (status,ident))
                return cursor.rowcount == 1
    '''),
    "taskdesk/service.py": source('''
        import json
        from pathlib import Path
        from .domain import validate_record

        class TaskService:
            def __init__(self, store):
                self.store = store

            def add_task(self, title, status="open", priority=0):
                return self.store.add(*validate_record(dict(title=title,status=status,priority=priority)))

            def list_tasks(self, status=None):
                if status is not None and status not in ("open", "done"):
                    raise ValueError("invalid status")
                return self.store.list_tasks(status)

            def import_json(self, path):
                records = json.loads(Path(path).read_text(encoding="utf-8"))
                if not isinstance(records, list):
                    raise ValueError("import requires a JSON list")
                for record in records:
                    self.store.add(*validate_record(record))
                return len(records)

            def complete(self, ident):
                return self.store.set_status(ident, "done")
    '''),
    "taskdesk/reporting.py": source('''
        import json

        def to_json(tasks):
            return json.dumps([task.to_dict() for task in tasks], ensure_ascii=False)
    '''),
    "taskdesk/settings.py": source('''
        from copy import deepcopy
        import json
        from pathlib import Path

        DEFAULTS = {"report": {"format": "json", "page_size": 20}, "display": {"columns": ["id", "title", "status"]}}

        def load_settings(path=None):
            settings = deepcopy(DEFAULTS)
            if path is not None:
                settings.update(json.loads(Path(path).read_text(encoding="utf-8")))
            return settings
    '''),
    "taskdesk/cli.py": source('''
        import argparse
        import json
        import sqlite3
        import sys
        from .storage import Store
        from .service import TaskService
        from .reporting import to_json

        def main(argv=None):
            parser = argparse.ArgumentParser(prog="taskdesk")
            parser.add_argument("--db", default="tasks.db")
            commands = parser.add_subparsers(dest="command", required=True)
            add = commands.add_parser("add")
            add.add_argument("title")
            add.add_argument("--priority", type=int, default=0)
            listing = commands.add_parser("list")
            listing.add_argument("--status", choices=("open", "done"))
            importing = commands.add_parser("import")
            importing.add_argument("path")
            complete = commands.add_parser("complete")
            complete.add_argument("id", type=int)
            args = parser.parse_args(argv)
            store = Store(args.db)
            service = TaskService(store)
            try:
                if args.command == "add":
                    print(json.dumps(service.add_task(args.title, priority=args.priority).to_dict()))
                elif args.command == "list":
                    print(to_json(service.list_tasks(args.status)))
                elif args.command == "import":
                    print(json.dumps({"imported": service.import_json(args.path)}))
                elif args.command == "complete":
                    print(json.dumps({"updated": service.complete(args.id)}))
                return 0
            except (ValueError, OSError, sqlite3.Error) as error:
                print(str(error), file=sys.stderr)
                return 2
            finally:
                store.close()
    '''),
    "tests/__init__.py": "",
    "tests/test_baseline.py": source('''
        import json
        from pathlib import Path
        import tempfile
        import unittest
        from taskdesk.domain import validate_record
        from taskdesk.storage import Store
        from taskdesk.service import TaskService
        from taskdesk.reporting import to_json
        from taskdesk.settings import load_settings

        class BaselineTests(unittest.TestCase):
            def setUp(self):
                self.tmp = tempfile.TemporaryDirectory()
                self.addCleanup(self.tmp.cleanup)
                self.store = Store(Path(self.tmp.name) / "tasks.db")
                self.addCleanup(self.store.close)
                self.service = TaskService(self.store)

            def test_add_and_complete(self):
                task = self.service.add_task(" Draft ", priority=2)
                self.assertEqual(task.title, "Draft")
                self.assertTrue(self.service.complete(task.id))
                self.assertFalse(self.service.complete(999))
                self.assertEqual(len(self.service.list_tasks("done")), 1)
                self.assertEqual(self.service.list_tasks("open"), [])

            def test_valid_import(self):
                path = Path(self.tmp.name) / "import.json"
                path.write_text(json.dumps([{"title": "a"}, {"title": "b", "status": "done"}]))
                self.assertEqual(self.service.import_json(path), 2)
                self.assertEqual([t.title for t in self.service.list_tasks()], ["a", "b"])

            def test_validation(self):
                for record in [{}, {"title": " "}, {"title": "x", "priority": True}, {"title": "x", "status": "bad"}]:
                    with self.subTest(record=record), self.assertRaises(ValueError):
                        validate_record(record)

            def test_json_and_settings(self):
                self.service.add_task("hello")
                self.assertEqual(json.loads(to_json(self.service.list_tasks()))[0]["title"], "hello")
                a = load_settings()
                a["display"]["columns"].append("priority")
                self.assertNotIn("priority", load_settings()["display"]["columns"])
    '''),
}

COMMON_GRADE = source('''
    import json, sqlite3, tempfile, subprocess, sys, unittest
    from pathlib import Path
    from taskdesk.storage import Store
    from taskdesk.service import TaskService

    def cli(db, *args):
        return subprocess.run([sys.executable, '-m', 'taskdesk', '--db', str(db), *args], capture_output=True, text=True, timeout=10)

    baseline = unittest.TestLoader().discover('tests')
    assert unittest.TextTestRunner().run(baseline).wasSuccessful()
''')

ATOMIC_GRADE = source('''
    with tempfile.TemporaryDirectory() as tmp:
        db, data = Path(tmp)/'tasks.db', Path(tmp)/'data.json'
        store = Store(db); service = TaskService(store)
        service.add_task('existing', priority=3)
        initial = [t.to_dict() for t in service.list_tasks()]
        bad_batches = [
            [{'title':'first'}, {'title':' '}],
            [{'title':'first'}, {'title':'existing'}],
            [{'title':'first'}, {'title':'first'}],
            [{'title':'first'}, {'title':'bad', 'priority':True}],
            [{'title':'first'}, {'title':'bad', 'status':'other'}],
            [{'title':'first'}, None], {},
        ]
        for payload in bad_batches:
            data.write_text(json.dumps(payload), encoding='utf-8')
            try: service.import_json(data)
            except (ValueError, sqlite3.Error): pass
            else: raise AssertionError('invalid import accepted')
            assert [t.to_dict() for t in service.list_tasks()] == initial, payload
            other = Store(db)
            assert [t.to_dict() for t in other.list_tasks()] == initial
            other.close()
        data.write_text('[', encoding='utf-8')
        result = cli(db, 'import', str(data))
        assert result.returncode == 2 and result.stderr.strip()
        data.write_text(json.dumps([{'title':'cli-first'}, {'title':'existing'}]))
        result = cli(db, 'import', str(data))
        assert result.returncode == 2 and result.stderr.strip()
        assert [t.to_dict() for t in service.list_tasks()] == initial
        data.write_text(json.dumps([{'title':' new ', 'status':'done', 'priority':5}, {'title':'second'}]))
        result = cli(db, 'import', str(data))
        assert result.returncode == 0 and json.loads(result.stdout) == {'imported':2}
        assert [t.title for t in service.list_tasks()] == ['existing','new','second']
        data.write_text('[]')
        assert service.import_json(data) == 0
        store.close()
''')

LISTING_GRADE = source('''
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp)/'tasks.db'
        store = Store(db); service = TaskService(store)
        records = [('low','open',0), ('closed','done',5), ('high-a','open',5), ('middle','open',2), ('high-b','open',5)]
        for title,status,priority in records: service.add_task(title,status,priority)
        assert [t.title for t in service.list_tasks()] == [r[0] for r in records]
        for api in (store,service):
            assert [t.title for t in api.list_tasks(status='open', sort='priority', limit=2, offset=1)] == ['high-b','middle']
            assert [t.title for t in api.list_tasks(sort='priority')] == ['closed','high-a','high-b','middle','low']
            assert api.list_tasks(limit=0) == []
            assert api.list_tasks(offset=99) == []
            assert [t.title for t in api.list_tasks(offset=3)] == ['middle','high-b']
        for kwargs in ({'limit':-1}, {'offset':-1}, {'sort':'oops'}):
            try: service.list_tasks(**kwargs)
            except ValueError: pass
            else: raise AssertionError(kwargs)
        result = cli(db,'list','--status','open','--sort','priority','--limit','2','--offset','1')
        assert result.returncode == 0, result.stderr
        output = json.loads(result.stdout)
        assert [r['title'] for r in output] == ['high-b','middle']
        assert set(output[0]) == {'id','title','status','priority'}
        for flags in [('--limit','-1'), ('--offset','-1'), ('--sort','oops')]:
            result = cli(db,'list',*flags)
            assert result.returncode == 2 and result.stderr.strip()
        assert len(service.list_tasks()) == 5
        store.close()
''')

UTILITIES_GRADE = source(r'''
    import csv, io, copy
    from taskdesk.domain import Task
    from taskdesk.reporting import to_csv, to_json
    from taskdesk.settings import load_settings, DEFAULTS
    tasks = [Task(2, 'comma, quote" and\nline 한글', 'open', 3), Task(1,'last','done',0)]
    original = copy.deepcopy(tasks)
    text = to_csv(iter(tasks))
    assert text.startswith('id,title,status,priority\n')
    rows = list(csv.reader(io.StringIO(text)))
    assert rows == [['id','title','status','priority'],['2',tasks[0].title,'open','3'],['1','last','done','0']]
    assert to_csv(iter([])) == 'id,title,status,priority\n'
    assert tasks == original and json.loads(to_json(tasks))[0]['id'] == 2
    with tempfile.TemporaryDirectory() as tmp:
        config = Path(tmp)/'config.json'
        config.write_text(json.dumps({'report':{'format':'csv'},'display':{'columns':['title']}}))
        defaults = copy.deepcopy(DEFAULTS)
        env = {'TASKDESK_PAGE_SIZE':'7'}
        result = load_settings(config, env=env)
        assert result == {'report':{'format':'csv','page_size':7},'display':{'columns':['title']}}
        result['display']['columns'].append('changed')
        assert DEFAULTS == defaults and env == {'TASKDESK_PAGE_SIZE':'7'}
        from unittest.mock import patch
        with patch.dict('os.environ', {'TASKDESK_PAGE_SIZE':'99'}):
            assert load_settings(config,env={})['report']['page_size'] == 20
            assert load_settings(config)['report']['page_size'] == 99
        for bad in ('0','-1','1001','abc',''):
            try: load_settings(config,env={'TASKDESK_PAGE_SIZE':bad})
            except ValueError: pass
            else: raise AssertionError(bad)
        for content in ('[]', 'null', '1'):
            config.write_text(content)
            try: load_settings(config,env={})
            except ValueError: pass
            else: raise AssertionError(content)
''')


def cases():
    specifications = {
        "atomic_import": (
            "Users report that a failing JSON import leaves earlier records saved. Investigate the CLI/service/storage flow and fix import_json so each import is all-or-nothing, including validation failures and duplicate titles either within the batch or against existing data. Previously committed tasks must survive failures, and no partial data may remain visible from a second connection. Preserve successful/empty imports, validation, public APIs, and CLI success JSON/error exit 2. Add regressions in test_atomic_import.py.",
            ATOMIC_GRADE, ["test_atomic_import.py"]),
        "paged_listing": (
            "Add task list sorting and pagination end to end. Extend Store.list_tasks and TaskService.list_tasks with keyword parameters sort='id', limit=None, offset=0. Keep default id-ascending ordering. sort='priority' orders priority descending then id ascending. Apply status filtering before sorting/pagination. Zero limit yields no rows; absent limit has no cap; offsets past the end return empty. Reject negative limit/offset or unsupported sort with ValueError from the service and CLI exit 2. Add list CLI flags --sort, --limit, --offset. Keep the existing JSON row format and do not mutate stored tasks. Document usage and add test_listing.py.",
            LISTING_GRADE, ["test_listing.py"]),
        "independent_utilities": (
            "Implement two independent library improvements; no CLI changes are needed. A: In taskdesk/reporting.py add to_csv(tasks), accepting a one-pass iterable of Task, preserving order, emitting header id,title,status,priority even for empty input, using newline line endings and correct CSV quoting for commas, quotes, newlines and Unicode. Preserve to_json and inputs; add test_csv_export.py. B: In taskdesk/settings.py extend load_settings(path=None, env=None). Recursively merge JSON object settings into independent copies of DEFAULTS. Non-dictionaries replace previous values; reject a non-object JSON root with ValueError. env=None uses os.environ; explicit env={} ignores the process environment. TASKDESK_PAGE_SIZE overrides report.page_size and must parse as integer 1..1000, otherwise ValueError. Preserve input mappings and defaults; add test_settings.py. Document both APIs.",
            UTILITIES_GRADE, ["test_csv_export.py", "test_settings.py"]),
    }
    result = {}
    for name, (prompt, grade, tests) in specifications.items():
        contract = COMMON_GRADE
        for path, text in FILES.items():
            if path.startswith("tests/"):
                contract += f"\nassert Path({path!r}).read_text(encoding='utf-8') == {text!r}\n"
        for path in tests:
            contract += f"\ntest_path = next((p for p in (Path({path!r}), Path('tests') / {path!r}) if p.is_file()), None)\nassert test_path is not None\nsuite = unittest.TestLoader().discover(str(test_path.parent), pattern=test_path.name)\nassert suite.countTestCases() > 0\nresult = unittest.TextTestRunner().run(suite)\nassert result.wasSuccessful() and result.testsRun > len(result.skipped)\n"
        result[name] = dict(files=dict(FILES), grade=contract + "\n" + grade,
                           prompt=prompt + " Use only the standard library. Preserve existing tests/ files byte-for-byte; run all existing and new tests. Keep changes within this project.")
    return result
