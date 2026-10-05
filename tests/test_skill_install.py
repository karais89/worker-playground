import importlib.util
import os
from pathlib import Path, PureWindowsPath
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("skill_installer", ROOT / "scripts/install_skill.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class SkillInstallTests(unittest.TestCase):
    def load_shallow_launcher(self, runtime_available):
        """Model a drive-root installation without accessing that filesystem."""
        class ShallowPath(PureWindowsPath):
            def resolve(self):
                return self

            def is_file(self):
                return runtime_available and self in {
                    PureWindowsPath(r'C:\cli-worker-team\scripts\runtime\team.py'),
                    PureWindowsPath(r'C:\cli-worker-team\scripts\runtime\worker.py'),
                    PureWindowsPath(r'C:\cli-worker-team\scripts\runtime\opencode_backend.py'),
                }

        launcher_spec = importlib.util.spec_from_file_location(
            'shallow_skill_launcher', ROOT / 'skills/cli-worker-team/scripts/run_team.py')
        launcher = importlib.util.module_from_spec(launcher_spec)
        launcher_spec.loader.exec_module(launcher)
        launcher.__file__ = r'C:\cli-worker-team\scripts\run_team.py'
        launcher.Path = ShallowPath
        self.assertEqual(len(ShallowPath(launcher.__file__).parents), 3)
        return launcher

    def test_shallow_installation_launches_bundled_runtime(self):
        launcher = self.load_shallow_launcher(runtime_available=True)
        runtime = types.ModuleType('team')
        runtime.main = Mock(return_value=17)
        with patch.dict(sys.modules, {'team': runtime}), patch.object(sys, 'path', list(sys.path)):
            self.assertEqual(launcher.main(), 17)
            self.assertEqual(sys.path[0], r'C:\cli-worker-team\scripts\runtime')
        runtime.main.assert_called_once_with()

    def test_shallow_installation_without_runtime_reports_explicit_error(self):
        launcher = self.load_shallow_launcher(runtime_available=False)
        runtime = types.ModuleType('team')
        runtime.main = Mock()
        with patch.dict(sys.modules, {'team': runtime}), patch.object(sys, 'path', list(sys.path)):
            with self.assertRaisesRegex(SystemExit, 'Runtime missing'):
                launcher.main()
        runtime.main.assert_not_called()

    def test_installed_snapshot_runs_outside_checkout_and_contains_only_allowlisted_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "skills with spaces" / "cli-worker-team"
            installer.install(target)
            files = {p.relative_to(target).as_posix() for p in target.rglob('*') if p.is_file()}
            self.assertEqual(files, set(installer.FILES) | {'installation.json'})
            for relative, source in installer.FILES.items():
                self.assertEqual((target / relative).read_bytes(), source.read_bytes())
            env = dict(os.environ)
            env.pop('PYTHONPATH', None)
            result = subprocess.run([sys.executable, '-I', str(target / 'scripts/run_team.py'), '--help'],
                                    cwd=tmp, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('--worker-model', result.stdout)
            self.assertIn('--worker-backend', result.stdout)
            self.assertIn('--opencode', result.stdout)
            runtime = subprocess.run([sys.executable, str(target / 'scripts/runtime/worker.py'), 'run', '--help'],
                                     cwd=tmp, env=env, capture_output=True, text=True)
            self.assertEqual(runtime.returncode, 0, runtime.stderr)
            self.assertIn('--backend', runtime.stdout)

    def test_existing_skill_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'existing'
            target.mkdir()
            (target / 'SKILL.md').write_text('user customization')
            with self.assertRaises(ValueError):
                installer.install(target)
            self.assertEqual((target / 'SKILL.md').read_text(), 'user customization')

    def test_copy_failure_does_not_leave_partial_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'skill'
            with patch.object(installer.shutil, 'copyfile', side_effect=OSError('disk failure')):
                with self.assertRaises(OSError):
                    installer.install(target)
            self.assertFalse(target.exists())
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_source_launcher_works_from_another_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            launcher = ROOT / 'skills/cli-worker-team/scripts/run_team.py'
            result = subprocess.run([sys.executable, '-I', str(launcher), '--help'], cwd=tmp,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('--main-model', result.stdout)


if __name__ == '__main__':
    unittest.main()
