#!/usr/bin/env bash
# Pulls the prebuilt glibc test-libs image (or builds it locally as a fallback)
# and extracts the artifacts.
#
# Usage: ./scripts/download-test-glibcs.sh
#
# Artifacts are extracted to tests/binaries/host/glibcs/<version>/
# Skips build if artifacts already exist.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
DEST="${REPO_ROOT}/tests/binaries/host/glibcs"

# Prebuilt artifact image to pull. TODO(upstream): change default to
# ghcr.io/pwndbg/glibc-test-libs:latest before merging to pwndbg/pwndbg.
GLIBC_IMAGE="${GLIBC_IMAGE:-ghcr.io/monthly-recurring-revenue/glibc-test-libs:latest}"

# Versions parsed from Dockerfile.glibc-test-libs (its build-<ver> stages).
mapfile -t EXPECTED_VERSIONS < <(sed -n 's/^FROM base-builder AS build-\([0-9][0-9.]*\).*/\1/p' "${REPO_ROOT}/Dockerfile.glibc-test-libs")
if [ "${#EXPECTED_VERSIONS[@]}" -eq 0 ]; then
    echo "ERROR: could not parse glibc versions from Dockerfile.glibc-test-libs" >&2
    exit 1
fi

# Check if all versions are already present
all_present=true
for ver in "${EXPECTED_VERSIONS[@]}"; do
    if [ ! -f "${DEST}/${ver}/libc.so.6" ] || [ ! -f "${DEST}/${ver}/ld-linux-x86-64.so.2" ]; then
        all_present=false
        break
    fi
done

if [ "${all_present}" = true ]; then
    echo "All glibc test artifacts already present in ${DEST}/"
    exit 0
fi

# Pull the prebuilt artifact image; fall back to building locally if it isn't
# published yet (or you're iterating on the Dockerfile / build script).
if docker pull "${GLIBC_IMAGE}"; then
    IMG="${GLIBC_IMAGE}"
else
    echo "Image ${GLIBC_IMAGE} unavailable, building locally (this may take a while)..."
    docker buildx build -f "${REPO_ROOT}/Dockerfile.glibc-test-libs" -t glibc-test-libs:local --load "${REPO_ROOT}"
    IMG="glibc-test-libs:local"
fi

# scratch image has no entrypoint, so give `docker create` one so it succeeds
CID=$(docker create --entrypoint=/ "${IMG}")
# remove the throwaway container even if `docker cp` fails under `set -e`
trap 'docker rm -f "${CID}" >/dev/null 2>&1 || true' EXIT
mkdir -p "${DEST}"
docker cp "${CID}:/glibcs/." "${DEST}/"

echo "Glibc test artifacts extracted to ${DEST}/"
missing=false
for ver in "${EXPECTED_VERSIONS[@]}"; do
    if [ -f "${DEST}/${ver}/libc.so.6" ] && [ -f "${DEST}/${ver}/ld-linux-x86-64.so.2" ]; then
        echo "  ${ver}: OK"
    else
        echo "  ${ver}: MISSING"
        missing=true
    fi
done
if [ "${missing}" = true ]; then
    echo "ERROR: some glibc versions are missing artifacts" >&2
    exit 1
fi
