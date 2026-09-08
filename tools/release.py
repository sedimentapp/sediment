"""Build, compose, verify and install immutable wheel bundles."""

import argparse
import hashlib
import importlib
import importlib.metadata
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(args, cwd):
    print(json.dumps({"operation": "command", "argv": [str(a) for a in args]}), flush=True)
    subprocess.run([str(a) for a in args], cwd=cwd, check=True)


def capture(args, cwd):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout


def write_manifest(bundle, manifest):
    (bundle / "release.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def verify(bundle):
    manifest = json.loads((bundle / "release.json").read_text())
    if manifest["format_version"] != 1 or manifest["verified"] is not True:
        raise ValueError("Unsupported or unverified bundle")
    required = {"requirements.txt", "test-requirements.txt", "release.py", "build-requirements.txt"}
    required.update(package["wheel"] for package in manifest["packages"])
    if not required <= manifest["files"].keys():
        raise ValueError("Bundle manifest is missing required files")
    for name, expected in manifest["files"].items():
        if Path(name).name != name or (bundle / name).is_symlink():
            raise ValueError(f"Invalid bundle path: {name}")
        if sha(bundle / name) != expected:
            raise ValueError(f"Bundle checksum mismatch: {name}")
    return manifest


def verify_public(manifest):
    if manifest["kind"] != "core" or manifest["origin"]["dirty"]:
        raise ValueError("Public promotion requires a clean core release")
    if {p["name"] for p in manifest["packages"]} != {"sediment", "knowledge-schema"}:
        raise ValueError("Public core contains unexpected packages")
    if set(manifest["expected_sources"]) != {"youtrack", "mattermost"}:
        raise ValueError("Public core contains unexpected source requirements")
    allowed = {"requirements.txt", "test-requirements.txt", "build-requirements.txt", "release.py"}
    allowed.update(p["wheel"] for p in manifest["packages"])
    if set(manifest["files"]) != allowed:
        raise ValueError("Public core contains unexpected files")


def check_installed(bundle):
    manifest = json.loads((bundle / "release.json").read_text())
    for package in manifest["packages"]:
        if importlib.metadata.version(package["name"]) != package["version"]:
            raise ValueError(f"Wrong installed version: {package['name']}")
        module = importlib.import_module(package["module"])
        if module.__file__ is None or not Path(module.__file__).is_relative_to(sys.prefix):
            raise ValueError(f"Module loaded outside installation: {package['module']}")
    registry = importlib.import_module("sediment.registry").available_sources()
    missing = set(manifest["expected_sources"]) - registry.keys()
    if missing:
        raise ValueError(f"Missing expected sources: {sorted(missing)}")
    help_text = capture([str(Path(sys.executable).parent / "raw-fetch"), "--help"], bundle)
    for source in manifest["expected_sources"]:
        if source not in help_text:
            raise ValueError(f"CLI source missing: {source}")
    print("Installed package versions, imports and sources verified")


def sync_environment(bundle, venv, requirements):
    python = venv / "bin/python"
    run(["uv", "pip", "sync", "--python", python, "--require-hashes", requirements], bundle)
    run(["uv", "pip", "check", "--python", python], bundle)
    run([python, "-B", bundle / "release.py", "check-installed", "--bundle", bundle], bundle)


def validate_bundle(bundle, tests):
    with tempfile.TemporaryDirectory(prefix="release-test-") as temp:
        venv = Path(temp) / "venv"
        run(["uv", "venv", "--python", sys.executable, venv], bundle)
        sync_environment(bundle, venv, "test-requirements.txt")
        run([venv / "bin/python", "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider", *tests], bundle)
        sync_environment(bundle, venv, "requirements.txt")


def provenance(root, allow_dirty):
    commit = capture(["git", "rev-parse", "HEAD"], root).strip()
    dirty = bool(capture(["git", "status", "--porcelain", "--untracked-files=normal"], root).strip())
    if dirty and not allow_dirty:
        raise ValueError("Release requires a clean checkout; --allow-dirty is only for local candidates")
    return {"commit": commit, "dirty": dirty}


def build_wheel(package, module, bundle, build_python):
    project = tomllib.loads((package / "pyproject.toml").read_text())["project"]
    before = set(bundle.glob("*.whl"))
    run(["uv", "build", "--wheel", "--no-create-gitignore", "--no-sources", "--no-build-isolation", "--python", build_python,
         "--out-dir", bundle, package], package)
    added = set(bundle.glob("*.whl")) - before
    if len(added) != 1:
        raise ValueError(f"Expected exactly one new wheel from {project['name']}")
    wheel = added.pop()
    return {"name": project["name"], "version": project["version"], "wheel": wheel.name,
            "module": module}


def export_requirements(root, package, test=False):
    args = ["uv", "export", "--frozen", "--package", package, "--no-default-groups", "--no-emit-local", "--no-header", "--no-annotate"]
    if test:
        args += ["--group", "test"]
    return capture(args, root)


def finish(bundle, manifest, tests):
    manifest["files"] = {p.name: sha(p) for p in sorted(bundle.iterdir()) if p.is_file() and p.name != "release.json"}
    manifest["verified"] = False
    write_manifest(bundle, manifest)
    validate_bundle(bundle, tests)
    manifest["verified"] = True
    write_manifest(bundle, manifest)
    verify(bundle)
    archive = bundle.parent / (bundle.name + ".tar.gz")
    with tarfile.open(archive, "x:gz") as tar:
        for path in sorted(bundle.iterdir()):
            if path.is_file():
                tar.add(path, arcname=path.name)
    digest = sha(archive)
    archive.with_suffix(archive.suffix + ".sha256").write_text(f"{digest}  {archive.name}\n")
    print(json.dumps({"artifact": str(archive), "sha256": digest, "version": manifest["version"]}))


def build(args):
    root = args.root.resolve()
    origin = provenance(root, args.allow_dirty)
    bundle = args.output.resolve()
    bundle.mkdir(parents=True, exist_ok=False)
    if args.command == "build-core":
        base = Path(__file__).resolve().parent
        manifest = {"format_version": 1, "kind": "core", "origin": origin, "expected_sources": ["youtrack", "mattermost"], "packages": []}
        shutil.copyfile(base / "build-requirements.txt", bundle / "build-requirements.txt")
        shutil.copyfile(__file__, bundle / "release.py")
        package_dirs = [(root / "packages/knowledge-schema", "knowledge_schema"), (root / "packages/sediment", "sediment")]
        runtime = export_requirements(root, "sediment")
        testing = export_requirements(root, "sediment", test=True)
        tests = [root / "packages/sediment/tests", root / "packages/knowledge-schema/tests"]
    else:
        core = args.core.resolve()
        core_manifest = verify(core)
        if core_manifest["kind"] != "core":
            raise ValueError("Composition requires a core bundle")
        if core_manifest["origin"]["dirty"] and not args.allow_dirty:
            raise ValueError("Dirty core is only allowed for an explicit local candidate")
        for name in core_manifest["files"]:
            shutil.copyfile(core / name, bundle / name)
        manifest = {"format_version": 1, "kind": "installation", "origin": origin,
                    "core_manifest_sha256": sha(core / "release.json"), "core_origin": core_manifest["origin"],
                    "expected_sources": [*core_manifest["expected_sources"], *args.expect_source],
                    "packages": list(core_manifest["packages"])}
        package = root / args.package
        name = tomllib.loads((package / "pyproject.toml").read_text())["project"]["name"]
        package_dirs = [(package, args.module)]
        runtime = (core / "requirements.txt").read_text() + "\n" + export_requirements(root, name)
        testing = (core / "test-requirements.txt").read_text() + "\n" + export_requirements(root, name)
        tests = [package / "tests"]
    with tempfile.TemporaryDirectory(prefix="release-build-") as temp:
        venv = Path(temp) / "venv"
        run(["uv", "venv", "--python", sys.executable, venv], bundle)
        run(["uv", "pip", "sync", "--python", venv / "bin/python", "--require-hashes", bundle / "build-requirements.txt"], bundle)
        for package, module in package_dirs:
            item = build_wheel(package, module, bundle, venv / "bin/python")
            manifest["packages"].append(item)
            requirement = f"\n./{item['wheel']} --hash=sha256:{sha(bundle / item['wheel'])}\n"
            runtime += requirement
            testing += requirement
    manifest["version"] = manifest["packages"][-1]["version"]
    manifest["python"] = {"major": sys.version_info.major, "minor": sys.version_info.minor, "build": sys.version}
    manifest["lock_sha256"] = sha(root / "uv.lock")
    (bundle / "requirements.txt").write_text(runtime)
    (bundle / "test-requirements.txt").write_text(testing)
    finish(bundle, manifest, tests)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("build-core", "compose"):
        command = sub.add_parser(name)
        command.add_argument("--root", type=Path, required=True)
        command.add_argument("--output", type=Path, required=True)
        command.add_argument("--allow-dirty", action="store_true")
        if name == "compose":
            command.add_argument("--core", type=Path, required=True)
            command.add_argument("--package", required=True)
            command.add_argument("--module", required=True)
            command.add_argument("--expect-source", action="append", required=True)
    for name in ("verify", "install", "check-installed"):
        command = sub.add_parser(name)
        command.add_argument("--bundle", type=Path, required=True)
        if name == "install":
            command.add_argument("--venv", type=Path, required=True)
        if name == "verify":
            command.add_argument("--public", action="store_true", help="Require a clean core-only release for publication")
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 14):
        parser.error("This release toolchain requires Python 3.14")
    if args.command in ("build-core", "compose"):
        build(args)
    elif args.command == "check-installed":
        check_installed(args.bundle.resolve())
    else:
        bundle = args.bundle.resolve()
        manifest = verify(bundle)
        if args.command == "verify" and args.public:
            verify_public(manifest)
        if args.command == "install":
            venv = args.venv.resolve()
            if venv.exists():
                raise FileExistsError(f"Refusing to overwrite {venv}")
            run(["uv", "venv", "--python", sys.executable, venv], bundle)
            sync_environment(bundle, venv, "requirements.txt")


if __name__ == "__main__":
    main()
