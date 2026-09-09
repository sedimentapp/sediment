import importlib.util
from pathlib import Path
import subprocess

import pytest

SPEC = importlib.util.spec_from_file_location("release_tag", Path(__file__).parents[1] / "check-release-tag.py")
assert SPEC is not None and SPEC.loader is not None
release_tag = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_tag)


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def checkout(tmp_path):
    git(tmp_path, "init")
    git(tmp_path, "config", "user.name", "Test")
    git(tmp_path, "config", "user.email", "test@example.com")
    for package in ("sediment", "knowledge-schema", "sediment-mcp"):
        path = tmp_path / "packages" / package / "pyproject.toml"
        path.parent.mkdir(parents=True)
        version = "0.1.0" if package == "sediment-mcp" else "0.2.0rc1"
        path.write_text(f'[project]\nversion = "{version}"\n')
    notes = tmp_path / "docs" / "release-notes-0.2.0rc1.md"
    notes.parent.mkdir()
    notes.write_text("Release candidate\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "Initial release")
    git(tmp_path, "tag", "-a", "v0.2.0rc1", "-m", "Candidate")
    return tmp_path


def test_annotated_tag_and_versions(checkout):
    result = release_tag.validate("v0.2.0rc1", checkout)
    assert result["commit"] == git(checkout, "rev-parse", "HEAD")
    assert result["notes"] == "docs/release-notes-0.2.0rc1.md"
    assert result["prerelease"] == "true"


def test_wrong_checkout_rejected(checkout):
    git(checkout, "commit", "--allow-empty", "-m", "Later commit")
    with pytest.raises(ValueError, match="Checkout does not match"):
        release_tag.validate("v0.2.0rc1", checkout)


def test_package_mismatch_rejected(checkout):
    (checkout / "packages/knowledge-schema/pyproject.toml").write_text('[project]\nversion = "0.1.0"\n')
    with pytest.raises(ValueError, match="knowledge-schema version"):
        release_tag.validate("v0.2.0rc1", checkout)


def test_missing_notes_rejected(checkout):
    (checkout / "docs/release-notes-0.2.0rc1.md").unlink()
    with pytest.raises(FileNotFoundError, match="Release notes missing"):
        release_tag.validate("v0.2.0rc1", checkout)


@pytest.mark.parametrize("tag", ["master", "refs/heads/master", "v1.0\nother=value", "v../../file"])
def test_invalid_tag_rejected(checkout, tag):
    with pytest.raises(ValueError, match="vVERSION"):
        release_tag.validate(tag, checkout)


def test_branch_with_version_name_is_not_a_tag(checkout):
    git(checkout, "branch", "v9.0.0")
    with pytest.raises(subprocess.CalledProcessError):
        release_tag.validate("v9.0.0", checkout)


def test_stable_release(checkout):
    for path in (checkout / "packages").glob("*/pyproject.toml"):
        path.write_text('[project]\nversion = "1.0.0"\n')
    (checkout / "docs/release-notes-1.0.0.md").write_text("Stable\n")
    git(checkout, "add", ".")
    git(checkout, "commit", "-m", "Stable release")
    git(checkout, "tag", "v1.0.0")
    assert release_tag.validate("v1.0.0", checkout)["prerelease"] == "false"
