"""Verify a core archive and its existing release tag before public promotion."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile

from release import verify, verify_public


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{64}", args.sha256):
        raise ValueError("A full SHA-256 digest is required")
    if hashlib.sha256(args.archive.read_bytes()).hexdigest() != args.sha256:
        raise ValueError("Archive checksum mismatch")
    if not re.fullmatch(r"v[0-9][0-9A-Za-z.+-]*", args.tag):
        raise ValueError("Invalid release tag")
    with tempfile.TemporaryDirectory(prefix="promote-core-") as temp:
        core = Path(temp)
        with tarfile.open(args.archive, "r:gz") as tar:
            names = []
            for member in tar.getmembers():
                if not member.isfile() or Path(member.name).name != member.name:
                    raise ValueError(f"Invalid archive member: {member.name}")
                names.append(member.name)
            if len(names) != len(set(names)):
                raise ValueError("Archive contains duplicate paths")
            tar.extractall(core, filter="data")
        manifest = verify(core)
        verify_public(manifest)
        if set(names) != set(manifest["files"]) | {"release.json"}:
            raise ValueError("Archive contains files outside the public manifest")
        if args.tag != f"v{manifest['version']}":
            raise ValueError("Tag does not match the bundled package version")
        commit = subprocess.run(["git", "rev-parse", f"refs/tags/{args.tag}^{{commit}}"],
                                check=True, capture_output=True, text=True).stdout.strip()
        if commit != manifest["origin"]["commit"]:
            raise ValueError("Release tag does not point to the tested source commit")
        output = args.output.resolve()
        output.mkdir(parents=True, exist_ok=False)
        artifact = output / "core.tar.gz"
        shutil.copyfile(args.archive, artifact)
        (output / "core.tar.gz.sha256").write_text(f"{args.sha256}  core.tar.gz\n")
        (output / "release.json").write_text(json.dumps(manifest, indent=2) + "\n")
        print(f"Prepared {args.tag} from tested commit {commit}; archive bytes unchanged")


if __name__ == "__main__":
    main()
