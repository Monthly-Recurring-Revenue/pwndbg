#!/usr/bin/env bash
# Build a single glibc version from source and package it for pwndbg heap testing.
#
# Usage: ./build-one-glibc.sh <version>
# Example: ./build-one-glibc.sh 2.43
#
# Output: /glibcs/<version>/ directory containing:
#   ld-<ver>.so, ld-linux-x86-64.so.2, libc-<ver>.so, libc.so.6,
#   .debug/libc-<ver>.so, and optionally libpthread files (glibc < 2.34)

set -euo pipefail

VERSION="${1:?Usage: $0 <glibc-version>}"
MAJOR="${VERSION%%.*}"
MINOR="${VERSION#*.}"

SRC_DIR="/tmp/glibc-src-${VERSION}"
BUILD_DIR="/tmp/glibc-build-${VERSION}"
INSTALL_DIR="/tmp/glibc-install-${VERSION}"
OUT_DIR="/glibcs/${VERSION}"

echo "=== Building glibc ${VERSION} ==="

# Download source
echo "[1/5] Downloading glibc-${VERSION}..."
wget -q "https://ftp.gnu.org/gnu/glibc/glibc-${VERSION}.tar.gz" -O "/tmp/glibc-${VERSION}.tar.gz"
mkdir -p "${SRC_DIR}"
tar xf "/tmp/glibc-${VERSION}.tar.gz" -C "${SRC_DIR}" --strip-components=1

# Configure (glibc requires out-of-tree build)
echo "[2/5] Configuring..."
mkdir -p "${BUILD_DIR}"
cd "${BUILD_DIR}"
"${SRC_DIR}/configure" \
    --prefix=/opt/glibc \
    --disable-werror \
    --enable-shared \
    --with-headers=/usr/include \
    CFLAGS="-g -O2"

# Build
echo "[3/5] Building (this takes a few minutes)..."
make -j"$(nproc)"

# Install to staging directory
echo "[4/5] Installing to staging..."
make install DESTDIR="${INSTALL_DIR}"

# Package artifacts
echo "[5/5] Packaging artifacts..."
mkdir -p "${OUT_DIR}/.debug"

# Find the actual library files in the install tree
LIBC_SO=$(find "${INSTALL_DIR}" -name "libc.so.6" -type f 2>/dev/null | head -1)
if [ -z "${LIBC_SO}" ]; then
    LIBC_SO=$(find "${INSTALL_DIR}" -name "libc-${VERSION}.so" -type f 2>/dev/null | head -1)
fi
if [ -z "${LIBC_SO}" ]; then
    # In newer glibc, libc.so.6 might be the actual file not a symlink
    LIBC_SO=$(find "${INSTALL_DIR}" -name "libc.so.6" 2>/dev/null | head -1)
fi

LD_SO=$(find "${INSTALL_DIR}" -name "ld-linux-x86-64.so.2" 2>/dev/null | head -1)
if [ -z "${LD_SO}" ]; then
    LD_SO=$(find "${INSTALL_DIR}" -name "ld-${VERSION}.so" 2>/dev/null | head -1)
fi

if [ -z "${LIBC_SO}" ] || [ -z "${LD_SO}" ]; then
    echo "ERROR: Could not find libc or ld in install tree"
    find "${INSTALL_DIR}" -name "libc*" -o -name "ld*" | head -20
    exit 1
fi

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

# For glibc < 2.34, libpthread was a separate library
if [ "${MINOR}" -lt 34 ]; then
    PTHREAD_SO=$(find "${INSTALL_DIR}" -name "libpthread.so.0" -o -name "libpthread-${VERSION}.so" | head -1)
    if [ -n "${PTHREAD_SO}" ]; then
        # Follow symlink to get the actual file
        PTHREAD_REAL=$(readlink -f "${PTHREAD_SO}")
        cp "${PTHREAD_REAL}" "${OUT_DIR}/libpthread-${VERSION}.so"
        ln -sf "libpthread-${VERSION}.so" "${OUT_DIR}/libpthread.so.0"
    fi
fi

# Cleanup build artifacts to save space
rm -rf "${SRC_DIR}" "${BUILD_DIR}" "${INSTALL_DIR}" "/tmp/glibc-${VERSION}.tar.gz"

echo "=== glibc ${VERSION} built successfully ==="
ls -la "${OUT_DIR}/"
ls -la "${OUT_DIR}/.debug/"
