#!/bin/sh
# Pinned toolchain for the build CI. Versions carry their SHA-256 so a tampered
# or swapped release fails the run instead of being executed.
set -eu

BIN_DIR="${HOME}/.local/bin"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT
mkdir -p "${BIN_DIR}"

download_verified() {
  url="$1"
  sha256="$2"
  output="$3"
  curl -fsSL --proto '=https' --tlsv1.2 -o "${output}" "${url}"
  printf '%s  %s\n' "${sha256}" "${output}" | sha256sum -c -
}

download_verified \
  "https://github.com/astral-sh/uv/releases/download/0.10.8/uv-x86_64-unknown-linux-gnu.tar.gz" \
  "f0c566b55683395a62fefb9261a060fa09824914b5682c3b9629fa154762ae2f" \
  "${TMP_DIR}/uv.tgz"
tar -xzf "${TMP_DIR}/uv.tgz" -C "${TMP_DIR}"
install -m 0755 "${TMP_DIR}/uv-x86_64-unknown-linux-gnu/uv" "${BIN_DIR}/uv"
install -m 0755 "${TMP_DIR}/uv-x86_64-unknown-linux-gnu/uvx" "${BIN_DIR}/uvx"

download_verified \
  "https://download.docker.com/linux/static/stable/x86_64/docker-27.5.1.tgz" \
  "4f798b3ee1e0140eab5bf30b0edc4e84f4cdb53255a429dc3bbae9524845d640" \
  "${TMP_DIR}/docker.tgz"
tar -xzf "${TMP_DIR}/docker.tgz" -C "${TMP_DIR}"
install -m 0755 "${TMP_DIR}/docker/docker" "${BIN_DIR}/docker"
