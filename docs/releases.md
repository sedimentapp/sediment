# Release bundles

The code repository builds a public core bundle. Installations compose their own
plugins with those exact wheels; they never rebuild core. The default core contains
only sediment and knowledge-schema, with YouTrack and Mattermost sources.

## Local candidates

Run with Python 3.14 and uv 0.10.8:

```sh
python tools/release.py build-core --root . --output /tmp/core-candidate --allow-dirty
```

Normal builds require a clean checkout. `--allow-dirty` is an explicit local-only
escape hatch, recorded in the manifest; such candidates cannot pass public promotion.
The builder installs the hashed `tools/build-requirements.txt` into an isolated
environment, builds wheels, installs them into a second clean environment and runs
the pipeline/schema tests against the installed packages. Runtime dependencies are
exported from the code repository's frozen uv.lock, with hashes. The result is a
directory, a `.tar.gz` archive, and an external SHA-256 file.

## Manifest v1

`release.json` records `format_version`, `kind` (core/installation), package names,
versions, wheel names and import modules; `expected_sources`; Python toolchain;
source commit/dirty status; lockfile SHA-256; and SHA-256 for every bundled file.
An installation also records its core manifest digest and core source provenance.
`verified` becomes true only after the clean-environment tests and runtime checks.
Hashes detect changed files but do not independently establish a trusted publisher.
Get the archive digest through the trusted release channel before executing its tools.

## Candidate distribution

Run the build and test steps in your CI, then retain the generated archive and
its SHA-256 in your artifact store. Configure credentials, storage endpoints and
site-specific automation in the installation repository. Pin the full archive
digest when composing an installation; do not replace an accepted artifact.

## Installation composition

After verifying/extracting a trusted core archive, use its own release tool:

```sh
python /path/to/core/release.py compose --core /path/to/core --root /path/to/installation \
  --package packages/my-source --module my_source --expect-source my-source --output /tmp/site-candidate
```

The installation package must be declared in its uv workspace/lockfile. Its runtime
dependencies are added to the core's exact requirements; conflicting pins fail
installation rather than changing core dependencies. Core wheels and build lock are
copied unchanged. The resulting bundle runs the plugin tests and verifies registered
sources. Local dirty candidates require `--allow-dirty` on this step too.

```sh
python /path/to/bundle/release.py install --bundle /path/to/bundle --venv /path/to/new-venv
```

Installation refuses an existing target and checks hashes, package versions,
imports, CLI sources and dependency compatibility. It does not switch systemd or
modify the active environment. It is not offline: third-party packages need an index
or cache. Preserve the tested archive; timestamped archive bytes are not promised
to be identical across rebuilds even with the same pinned build dependencies.

## Public promotion

First validate the candidate on the installation and complete its migrations.
Review the code for public suitability, then publish the matching code commit and
version tag to GitHub through the existing approved process. Public promotion does
not copy the installation repository or rebuild wheels.

The manual `.github/workflows/release.yml` takes the tested archive URL, its SHA-256,
and an existing `vVERSION` tag. It needs `CORE_BUNDLE_USER` and a read-only
`CORE_BUNDLE_READ_TOKEN` to download from an authenticated artifact store. `tools/prepare-release.py` verifies
the checksum, manifest, core-only file/package set, clean source provenance, version,
and tag commit. It creates a draft GitHub release containing the exact tested archive.
The composition checks do not prove absence of sensitive text inside allowed files:
public code review remains necessary. Review the draft and release notes before
publishing. Enable GitHub release immutability for accepted public releases.

Version package manifests together for a core release. The pipeline's dependency
on knowledge-schema is exact; plugin versions are independent. Update the release
notes and workflow notes-file when selecting the next release version.
