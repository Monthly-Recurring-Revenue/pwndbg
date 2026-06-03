#!/usr/bin/env bash
# Build a single glibc version from source and package it for pwndbg heap testing.
#
# Usage: ./build-one-glibc.sh <version>
# Example: ./build-one-glibc.sh 2.43
#
# Output: /glibcs/<version>/ directory containing:
#   ld-<ver>.so, ld-linux-x86-64.so.2, libc-<ver>.so, libc.so.6,
#   .debug/libc-<ver>.so

set -euo pipefail

VERSION="${1:?Usage: $0 <glibc-version>}"

SRC_DIR="/tmp/glibc-src-${VERSION}"
BUILD_DIR="/tmp/glibc-build-${VERSION}"
INSTALL_DIR="/tmp/glibc-install-${VERSION}"
OUT_DIR="/glibcs/${VERSION}"

echo "=== Building glibc ${VERSION} ==="

# Download source. BuildKit runs every version stage in parallel, so they hit the
# mirror at once; jitter the start to spread out that thundering herd.
DELAY=$((RANDOM % 30))
echo "[1/5] Downloading glibc-${VERSION} (delay ${DELAY}s)..."
sleep "${DELAY}"
TARBALL="/tmp/glibc-${VERSION}.tar.gz"
# ftp.gnu.org rate-limits hard under that parallel load, so try the load-balancing
# redirector and a reliable CDN mirror first and fall back to the canonical host.
# Each mirror gets several retries with backoff before falling through to the next.
MIRRORS=(
    "https://ftpmirror.gnu.org/glibc/glibc-${VERSION}.tar.gz"
    "https://mirrors.kernel.org/gnu/glibc/glibc-${VERSION}.tar.gz"
    "https://ftp.gnu.org/gnu/glibc/glibc-${VERSION}.tar.gz"
)
downloaded=
for url in "${MIRRORS[@]}"; do
    echo "Fetching ${url}"
    if wget -q --retry-connrefused --retry-on-host-error --waitretry=15 \
            --tries=5 --timeout=60 "${url}" -O "${TARBALL}"; then
        downloaded=1
        break
    fi
    echo "  mirror failed, trying next..."
done
[ -n "${downloaded}" ] || { echo "ERROR: all glibc ${VERSION} mirrors failed"; exit 4; }
mkdir -p "${SRC_DIR}"
tar xf "${TARBALL}" -C "${SRC_DIR}" --strip-components=1

# glibc requires an out-of-tree build (separate BUILD_DIR)
echo "[2/5] Configuring..."
mkdir -p "${BUILD_DIR}"
cd "${BUILD_DIR}"
CONFIGURE_LOG="/tmp/glibc-configure-${VERSION}.log"
if ! "${SRC_DIR}/configure" \
    --prefix=/opt/glibc \
    --disable-werror \
    --enable-shared \
    --with-headers=/usr/include \
    CFLAGS="-g -O2" > "${CONFIGURE_LOG}" 2>&1; then
    echo "CONFIGURE FAILED for glibc ${VERSION}. Last 30 lines:"
    tail -30 "${CONFIGURE_LOG}"
    exit 2
fi
echo "Configure completed successfully."

# Build with -k (keep going) so non-essential targets like support/links-dso-program
# that fail due to host toolchain mismatches don't block the important libraries
echo "[3/5] Building (this takes a few minutes)..."
BUILD_LOG="/tmp/glibc-build-${VERSION}.log"
make -j"$(nproc)" -k > "${BUILD_LOG}" 2>&1 || echo "Build had non-fatal errors (expected for old glibc on newer host)."

# Verify the critical output was actually produced
if [ ! -f "${BUILD_DIR}/libc.so.6" ] && [ ! -f "${BUILD_DIR}/libc.so" ]; then
    echo "FATAL: libc.so not produced by build. Last 50 lines of build log:"
    tail -50 "${BUILD_LOG}"
    exit 1
fi
echo "Build completed."

# Install with -k to skip any targets that weren't built
echo "[4/5] Installing to staging..."
make install -k DESTDIR="${INSTALL_DIR}" > /dev/null 2>&1 || true

# Package artifacts
echo "[5/5] Packaging artifacts..."
mkdir -p "${OUT_DIR}/.debug"

# Find libc.so.6 in the install tree (may be a symlink)
LIBC_SO=$(find "${INSTALL_DIR}" -name "libc.so.6" 2>/dev/null | head -1)
if [ -n "${LIBC_SO}" ]; then
    LIBC_SO=$(readlink -f "${LIBC_SO}")
fi

# Find ld-linux-x86-64.so.2 in install tree, then build tree as fallback
LD_SO=$(find "${INSTALL_DIR}" -name "ld-linux-x86-64.so.2" 2>/dev/null | head -1)
if [ -z "${LD_SO}" ]; then
    # Try ld.so in the install tree (different name)
    LD_SO=$(find "${INSTALL_DIR}" -name "ld-${VERSION}.so" 2>/dev/null | head -1)
fi
if [ -z "${LD_SO}" ]; then
    # Fallback: grab ld.so directly from the build tree
    echo "ld not found in install tree, checking build tree..."
    LD_SO="${BUILD_DIR}/elf/ld.so"
fi
if [ -n "${LD_SO}" ]; then
    LD_SO=$(readlink -f "${LD_SO}")
fi

if [ -z "${LIBC_SO}" ] || [ -z "${LD_SO}" ] || [ ! -f "${LIBC_SO}" ] || [ ! -f "${LD_SO}" ]; then
    echo "ERROR: Could not find libc or ld"
    echo "LIBC_SO=${LIBC_SO:-empty}"
    echo "LD_SO=${LD_SO:-empty}"
    find "${INSTALL_DIR}" -name "libc*" -o -name "ld*" 2>/dev/null | head -20
    find "${BUILD_DIR}" -name "ld.so" -o -name "ld-linux*" 2>/dev/null | head -10
    exit 1
fi

echo "Found libc: ${LIBC_SO}"
echo "Found ld: ${LD_SO}"

# Copy the dynamic linker
cp "${LD_SO}" "${OUT_DIR}/ld-${VERSION}.so"
ln -sf "ld-${VERSION}.so" "${OUT_DIR}/ld-linux-x86-64.so.2"

# Extract debug info from libc, then strip it
cp "${LIBC_SO}" "${OUT_DIR}/.debug/libc-${VERSION}.so"
objcopy --only-keep-debug "${OUT_DIR}/.debug/libc-${VERSION}.so"

cp "${LIBC_SO}" "${OUT_DIR}/libc-${VERSION}.so"
strip "${OUT_DIR}/libc-${VERSION}.so"
# Add a .gnu_debuglink so GDB finds the debug file
objcopy --add-gnu-debuglink="${OUT_DIR}/.debug/libc-${VERSION}.so" "${OUT_DIR}/libc-${VERSION}.so"

ln -sf "libc-${VERSION}.so" "${OUT_DIR}/libc.so.6"

# Optional no-debug variant (GLIBC_BUILD_NODEBUG=1): the same libc with the
# separate debug info and the .gnu_debuglink removed, emitted to a parallel tree so
# the debug-enabled artifacts above are untouched. With no debug file for GDB to
# load, pwndbg has no main_arena symbol to shortcut on, so its heap heuristic must
# actually scan .data/relocations, and version() must fall back to the .rodata
# "GNU C Library" banner. This is what the *-nodebug heuristic test exercises.
if [ "${GLIBC_BUILD_NODEBUG:-0}" = "1" ]; then
    ND_DIR="/glibcs-nodebug/${VERSION}"
    mkdir -p "${ND_DIR}"
    cp "${OUT_DIR}/ld-${VERSION}.so" "${ND_DIR}/ld-${VERSION}.so"
    ln -sf "ld-${VERSION}.so" "${ND_DIR}/ld-linux-x86-64.so.2"
    cp "${OUT_DIR}/libc-${VERSION}.so" "${ND_DIR}/libc-${VERSION}.so"
    objcopy --remove-section=.gnu_debuglink --strip-all "${ND_DIR}/libc-${VERSION}.so"
    ln -sf "libc-${VERSION}.so" "${ND_DIR}/libc.so.6"
    echo "=== glibc ${VERSION} no-debug variant ==="
    ls -la "${ND_DIR}/"
fi

# Cleanup build artifacts to save space
rm -rf "${SRC_DIR}" "${BUILD_DIR}" "${INSTALL_DIR}" "${TARBALL}"

echo "=== glibc ${VERSION} built successfully ==="
ls -la "${OUT_DIR}/"
ls -la "${OUT_DIR}/.debug/"
