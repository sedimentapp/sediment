"""Check the checked-out release tag before building or publishing artifacts."""

import argparse
import json
from pathlib import Path
import re
import subprocess
import tomllib


def validate(tag: str, root: Path) -> dict[str, str]:
    if not re.fullmatch(r"v[0-9][0-9A-Za-z.+-]*", tag):
        raise ValueError("Release requires a vVERSION tag")
    version = tag[1:]
    commit = subprocess.run(
        ["git", "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}"],
        cwd=root, check=True, capture_output=True, text=True,
    ).stdout.strip()
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True,
    ).stdout.strip()
    if head != commit:
        raise ValueError("Checkout does not match the release tag")
    for package in ("sediment", "knowledge-schema", "sediment-mcp"):
        project = tomllib.loads((root / "packages" / package / "pyproject.toml").read_text())["project"]
        if project["version"] != version:
            raise ValueError(f"Tag version {version} differs from {package} version {project['version']}")
    notes = f"docs/release-notes-{version}.md"
    if not (root / notes).is_file():
        raise FileNotFoundError(f"Release notes missing: {notes}")
    return {
        "tag": tag, "version": version, "commit": commit, "notes": notes,
        "prerelease": "false" if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) else "true",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    result = validate(args.tag, args.root)
    if args.github_output is not None:
        with args.github_output.open("a") as output:
            for key, value in result.items():
                output.write(f"{key}={value}\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
