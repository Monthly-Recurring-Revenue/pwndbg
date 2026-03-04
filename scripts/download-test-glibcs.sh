#!/usr/bin/env bash
# Downloads pre-built glibc test libraries from the Docker image on ghcr.io.
#
# Usage: ./scripts/download-test-glibcs.sh
#
# Artifacts are extracted to tests/binaries/host/glibcs/<version>/
# Skips download if artifacts already exist.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
DEST="${REPO_ROOT}/tests/binaries/host/glibcs"

IMAGE="${GLIBC_TEST_LIBS_IMAGE:-ghcr.io/pwndbg/glibc-test-libs:latest}"

# All glibc versions we expect to find in the image
EXPECTED_VERSIONS=(2.35 2.37 2.38 2.39 2.41 2.42 2.43)

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

echo "Downloading glibc test artifacts from ${IMAGE}..."
docker pull "${IMAGE}"

CID=$(docker create "${IMAGE}" /bin/true 2>/dev/null || docker create "${IMAGE}")
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
