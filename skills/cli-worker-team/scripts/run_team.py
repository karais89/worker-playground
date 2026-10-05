"""Launch the installed runtime, or the runtime in this source checkout."""
from pathlib import Path
import sys


def main():
    script = Path(__file__).resolve()
    bundled = script.parent / "runtime"
    runtime = bundled
    if not (bundled / "team.py").is_file() and len(script.parents) > 3:
        runtime = script.parents[3]
    if not all((runtime / name).is_file() for name in ("team.py", "worker.py")):
        raise SystemExit("Runtime missing. Install from the repository with: python scripts/install_skill.py")
    sys.path.insert(0, str(runtime))
    from team import main as run
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
