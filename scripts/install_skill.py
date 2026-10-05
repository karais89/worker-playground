"""Install a self-contained skill snapshot without copying credentials or logs."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile

ROOT = Path(__file__).resolve().parents[1]
NAME = "cli-worker-team"
FILES = {
    "SKILL.md": ROOT / "skills" / NAME / "SKILL.md",
    "agents/openai.yaml": ROOT / "skills" / NAME / "agents/openai.yaml",
    "scripts/run_team.py": ROOT / "skills" / NAME / "scripts/run_team.py",
    "scripts/runtime/team.py": ROOT / "team.py",
    "scripts/runtime/worker.py": ROOT / "worker.py",
    "scripts/runtime/opencode_backend.py": ROOT / "opencode_backend.py",
    "LICENSE": ROOT / "LICENSE",
}


def install(destination):
    destination = Path(destination).expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError(f"Destination already exists; preserve or remove it explicitly before installing: {destination}")
    for source in FILES.values():
        if not source.is_file():
            raise ValueError(f"Required source is missing: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Stage on the destination filesystem; a failed copy never creates a partial skill.
    with tempfile.TemporaryDirectory(prefix=".cli-worker-team-install-", dir=destination.parent) as tmp:
        staged = Path(tmp) / NAME
        staged.mkdir()
        hashes = {}
        for relative, source in FILES.items():
            target = staged / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            hashes[relative] = hashlib.sha256(target.read_bytes()).hexdigest()
        (staged / "installation.json").write_text(json.dumps(
            {"name": NAME, "files_sha256": hashes}, indent=2) + "\n", encoding="utf-8")
        # rename refuses existing nonempty directories; also recheck a racing install.
        if destination.exists() or destination.is_symlink():
            raise ValueError(f"Destination appeared during installation: {destination}")
        staged.rename(destination)
    return destination


def main():
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, default=home / "skills" / NAME,
                        help="Full skill directory; must not already exist")
    args = parser.parse_args()
    try:
        print(install(args.destination))
        return 0
    except (OSError, ValueError) as error:
        parser.exit(1, f"install: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
