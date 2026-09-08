import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("release", Path(__file__).parents[1] / "release.py")
assert SPEC is not None and SPEC.loader is not None
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


@pytest.fixture
def bundle(tmp_path):
    manifest = {"format_version": 1, "verified": True, "kind": "core",
                "origin": {"commit": "a" * 40, "dirty": False},
                "expected_sources": ["youtrack", "mattermost"],
                "packages": [{"name": "sediment", "wheel": "sediment.whl"},
                             {"name": "knowledge-schema", "wheel": "knowledge_schema.whl"}], "files": {}}
    for name in ("release.py", "requirements.txt", "test-requirements.txt", "build-requirements.txt", "sediment.whl", "knowledge_schema.whl"):
        (tmp_path / name).write_text("test file")
        manifest["files"][name] = hashlib.sha256(b"test file").hexdigest()
    (tmp_path / "release.json").write_text(json.dumps(manifest))
    return tmp_path, manifest


def test_changed_wheel_is_rejected(bundle):
    path, _ = bundle
    (path / "sediment.whl").write_text("changed wheel")
    with pytest.raises(ValueError, match="checksum"):
        release.verify(path)


def test_unfinished_build_cannot_be_installed(bundle):
    path, manifest = bundle
    manifest["verified"] = False
    (path / "release.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="unverified"):
        release.verify(path)


def test_missing_wheel_in_manifest_is_rejected(bundle):
    path, manifest = bundle
    del manifest["files"]["sediment.whl"]
    (path / "release.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="missing required"):
        release.verify(path)


def test_dirty_core_cannot_be_promoted(bundle):
    _, manifest = bundle
    manifest["origin"]["dirty"] = True
    with pytest.raises(ValueError, match="clean core"):
        release.verify_public(manifest)


def test_composed_bundle_cannot_be_promoted(bundle):
    _, manifest = bundle
    manifest["kind"] = "installation"
    with pytest.raises(ValueError, match="clean core"):
        release.verify_public(manifest)


def test_extra_file_cannot_be_promoted(bundle):
    _, manifest = bundle
    manifest["files"]["private.txt"] = "a" * 64
    with pytest.raises(ValueError, match="unexpected files"):
        release.verify_public(manifest)


def test_clean_core_passes_promotion_gate(bundle):
    path, _ = bundle
    release.verify_public(release.verify(path))
