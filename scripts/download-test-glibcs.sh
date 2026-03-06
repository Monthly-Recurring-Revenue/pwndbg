#!/usr/bin/env bash
# Builds glibc test libraries locally using Docker and extracts them.
#
# Usage: ./scripts/download-test-glibcs.sh
#
# Artifacts are extracted to tests/binaries/host/glibcs/<version>/
# Skips build if artifacts already exist.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
DEST="${REPO_ROOT}/tests/binaries/host/glibcs"

# All glibc versions we expect
EXPECTED_VERSIONS=(2.35 2.36 2.37 2.38 2.39 2.40 2.41 2.42 2.43)

# Check if all versions are already present
all_present=true
for ver in "${EXPECTED_VERSIONS[@]}"; do
    if [ ! -f "${DEST}/${ver}/libc.so.6" ]; then
        all_present=false
        break
    fi
done

if [ "${all_present}" = true ]; then
    echo "All glibc test artifacts already present in ${DEST}/"
    exit 0
fi

echo "Building glibc test libraries (this may take a while on first run)..."
docker buildx build -f "${REPO_ROOT}/Dockerfile.glibc-test-libs" -t glibc-test-libs:local --load "${REPO_ROOT}"

CID=$(docker create --entrypoint=/ glibc-test-libs:local)
mkdir -p "${DEST}"
docker cp "${CID}:/glibcs/." "${DEST}/"
docker rm "${CID}" > /dev/null

echo "Glibc test artifacts extracted to ${DEST}/"
for ver in "${EXPECTED_VERSIONS[@]}"; do
    if [ -f "${DEST}/${ver}/libc.so.6" ]; then
        echo "  ${ver}: OK"
    else
        echo "  ${ver}: MISSING"
    fi
done
