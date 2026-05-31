#!/usr/bin/env bash
# Builds musl test libraries locally using Docker and extracts them.
#
# Usage: ./scripts/download-test-musls.sh
#
# Artifacts are extracted to tests/binaries/host/musls/<version>/
# Skips build if artifacts already exist.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
DEST="${REPO_ROOT}/tests/binaries/host/musls"

# Prebuilt artifact image to pull. TODO(upstream): change default to
# ghcr.io/pwndbg/musl-test-libs:latest before merging to pwndbg/pwndbg.
MUSL_IMAGE="${MUSL_IMAGE:-ghcr.io/monthly-recurring-revenue/musl-test-libs:latest}"

# Versions parsed from Dockerfile.musl-test-libs (its build-<ver> stages).
mapfile -t EXPECTED_VERSIONS < <(sed -n 's/^FROM base-builder AS build-\([0-9][0-9.]*\).*/\1/p' "${REPO_ROOT}/Dockerfile.musl-test-libs")
if [ "${#EXPECTED_VERSIONS[@]}" -eq 0 ]; then
    echo "ERROR: could not parse musl versions from Dockerfile.musl-test-libs" >&2
    exit 1
fi

# Check if all versions are already present
all_present=true
for ver in "${EXPECTED_VERSIONS[@]}"; do
    if [ ! -f "${DEST}/${ver}/lib/libc.a" ] || [ ! -f "${DEST}/${ver}/lib/libc.so" ]; then
        all_present=false
        break
    fi
done

if [ "${all_present}" = true ]; then
    echo "All musl test artifacts already present in ${DEST}/"
    exit 0
fi

# Pull the prebuilt artifact image; fall back to building locally if it isn't
# published yet (or you're iterating on the Dockerfile / build script).
if docker pull "${MUSL_IMAGE}"; then
    IMG="${MUSL_IMAGE}"
else
    echo "Image ${MUSL_IMAGE} unavailable, building locally (this may take a while)..."
    docker buildx build -f "${REPO_ROOT}/Dockerfile.musl-test-libs" -t musl-test-libs:local --load "${REPO_ROOT}"
    IMG="musl-test-libs:local"
fi

# scratch image has no entrypoint, so give `docker create` one so it succeeds
CID=$(docker create --entrypoint=/ "${IMG}")
# remove the throwaway container even if `docker cp` fails under `set -e`
trap 'docker rm -f "${CID}" >/dev/null 2>&1 || true' EXIT
mkdir -p "${DEST}"
docker cp "${CID}:/musls/." "${DEST}/"

echo "Musl test artifacts extracted to ${DEST}/"
missing=false
for ver in "${EXPECTED_VERSIONS[@]}"; do
    if [ -f "${DEST}/${ver}/lib/libc.a" ] && [ -f "${DEST}/${ver}/lib/libc.so" ]; then
        echo "  ${ver}: OK"
    else
        echo "  ${ver}: MISSING"
        missing=true
    fi
done
if [ "${missing}" = true ]; then
    echo "ERROR: some musl versions are missing artifacts" >&2
    exit 1
fi
