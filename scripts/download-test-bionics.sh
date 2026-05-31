#!/usr/bin/env bash
# Pulls (or builds) the pre-built static bionic test binaries and extracts them.
# Mirrors scripts/download-test-musls.sh.
#
# Usage: ./scripts/download-test-bionics.sh
#
# Binaries are extracted to tests/binaries/host/bionics/<API>/
# Skips the pull/build if binaries already exist.
#
# Unlike the glibc/musl scripts, this ships pre-built test *binaries* (the NDK
# clang needed to compile them is not in the pwndbg test container), so there is
# no makefile rule -- the test consumes these extracted binaries directly.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
DEST="${REPO_ROOT}/tests/binaries/host/bionics"

# Prebuilt artifact image to pull. TODO(upstream): change default to
# ghcr.io/pwndbg/bionic-test-libs:latest before merging to pwndbg/pwndbg.
BIONIC_IMAGE="${BIONIC_IMAGE:-ghcr.io/monthly-recurring-revenue/bionic-test-libs:latest}"

# API levels parsed from Dockerfile.bionic-test-libs (its build-<API> stages) so
# the list lives in one place.
mapfile -t EXPECTED_APIS < <(sed -n 's/^FROM base-builder AS build-\([0-9][0-9.]*\).*/\1/p' "${REPO_ROOT}/Dockerfile.bionic-test-libs")
if [ "${#EXPECTED_APIS[@]}" -eq 0 ]; then
    echo "ERROR: could not parse API levels from Dockerfile.bionic-test-libs" >&2
    exit 1
fi

# Check if all API binaries are already present
all_present=true
for api in "${EXPECTED_APIS[@]}"; do
    if [ ! -f "${DEST}/${api}/bionic_probe.bionic-${api}-static.out" ]; then
        all_present=false
        break
    fi
done

if [ "${all_present}" = true ]; then
    echo "All bionic test binaries already present in ${DEST}/"
    exit 0
fi

# Pull the prebuilt artifact image; fall back to building locally if it isn't
# published yet (or you're iterating on the Dockerfile / build script).
if docker pull "${BIONIC_IMAGE}"; then
    IMG="${BIONIC_IMAGE}"
else
    echo "Image ${BIONIC_IMAGE} unavailable, building locally (this may take a while)..."
    docker buildx build -f "${REPO_ROOT}/Dockerfile.bionic-test-libs" -t bionic-test-libs:local --load "${REPO_ROOT}"
    IMG="bionic-test-libs:local"
fi

# scratch image has no entrypoint, so give `docker create` one so it succeeds
CID=$(docker create --entrypoint=/ "${IMG}")
# remove the throwaway container even if `docker cp` fails under `set -e`
trap 'docker rm -f "${CID}" >/dev/null 2>&1 || true' EXIT
mkdir -p "${DEST}"
docker cp "${CID}:/bionics/." "${DEST}/"

echo "Bionic test binaries extracted to ${DEST}/"
missing=false
for api in "${EXPECTED_APIS[@]}"; do
    if [ -f "${DEST}/${api}/bionic_probe.bionic-${api}-static.out" ]; then
        echo "  ${api}: OK"
    else
        echo "  ${api}: MISSING"
        missing=true
    fi
done
if [ "${missing}" = true ]; then
    echo "ERROR: some bionic API binaries are missing" >&2
    exit 1
fi
