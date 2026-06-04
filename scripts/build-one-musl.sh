#!/usr/bin/env bash
# Build a single musl libc version from source and package it for pwndbg testing.
#
# Usage: ./build-one-musl.sh <version>
# Example: ./build-one-musl.sh 1.2.5
#
# Output: /musls/<version>/ containing:
#   lib/libc.a              (static archive, for static test binaries)
#   lib/libc.so             (the real shared object = the dynamic loader; SONAME libc.so)
#   lib/ld-musl-x86_64.so.1 (symlink -> libc.so; musl's libc and ld are one file)
#   lib/{crt1,Scrt1,crti,crtn}.o
#   include/                (this version's headers)
#
# Built with --enable-debug and NOT fully stripped so the internal __libc_version
# symbol survives -- pwndbg's musl provider reads it for version detection.

set -euo pipefail

VERSION="${1:?Usage: $0 <musl-version>}"

# musl's loader symlink name is arch-specific; this script builds natively for the
# host arch (the x86-64 or aarch64 CI runner).
case "$(uname -m)" in
    x86_64)  MUSL_LD="ld-musl-x86_64.so.1" ;;
    aarch64) MUSL_LD="ld-musl-aarch64.so.1" ;;
    *) echo "FATAL: unsupported arch $(uname -m)" >&2; exit 1 ;;
esac

SRC_DIR="/tmp/musl-src-${VERSION}"
INSTALL_DIR="/opt/musl-${VERSION}"
OUT_DIR="/musls/${VERSION}"

echo "=== Building musl ${VERSION} ==="

# Download source (random delay to avoid thundering herd when BuildKit runs stages
# in parallel)
DELAY=$((RANDOM % 10))
echo "[1/5] Downloading musl-${VERSION} (delay ${DELAY}s)..."
sleep "${DELAY}"
TARBALL="/tmp/musl-${VERSION}.tar.gz"
URL="https://musl.libc.org/releases/musl-${VERSION}.tar.gz"
MIRROR="https://git.musl-libc.org/cgit/musl/snapshot/v${VERSION}.tar.gz"
if ! wget -q --retry-connrefused --waitretry=10 --tries=3 --timeout=60 "${URL}" -O "${TARBALL}"; then
    echo "Primary download failed, trying mirror..."
    wget -q --retry-connrefused --waitretry=10 --tries=3 --timeout=60 "${MIRROR}" -O "${TARBALL}"
fi
mkdir -p "${SRC_DIR}"
tar xf "${TARBALL}" -C "${SRC_DIR}" --strip-components=1

# Configure + build (musl builds in-tree)
echo "[2/5] Configuring..."
cd "${SRC_DIR}"
CONFIGURE_LOG="/tmp/musl-configure-${VERSION}.log"
if ! ./configure --prefix="${INSTALL_DIR}" --enable-debug > "${CONFIGURE_LOG}" 2>&1; then
    echo "CONFIGURE FAILED for musl ${VERSION}. Last 30 lines:"
    tail -30 "${CONFIGURE_LOG}"
    exit 2
fi

echo "[3/5] Building..."
make -j"$(nproc)" > "/tmp/musl-build-${VERSION}.log" 2>&1

echo "[4/5] Installing to ${INSTALL_DIR}..."
make install > /dev/null 2>&1

# Package artifacts
echo "[5/5] Packaging artifacts..."
mkdir -p "${OUT_DIR}/lib" "${OUT_DIR}/include"
cp -a "${INSTALL_DIR}/lib/libc.a" "${OUT_DIR}/lib/"
# musl's libc and dynamic loader are one file. Package it as libc.so (the real
# shared object) with ld-musl-x86_64.so.1 as the loader symlink -- the layout
# distro musl uses. Crucially, give it a SONAME: a binary linked against it then
# records DT_NEEDED=libc.so (a name), not an absolute path. Without a SONAME the
# linker bakes in the absolute path, which musl resolves to the interpreter
# itself, so no separate libc maps at runtime and pwndbg can't detect it.
cp -a "${INSTALL_DIR}/lib/libc.so" "${OUT_DIR}/lib/"
patchelf --set-soname libc.so "${OUT_DIR}/lib/libc.so"
ln -sf "libc.so" "${OUT_DIR}/lib/${MUSL_LD}"
cp -a "${INSTALL_DIR}/lib/"crt1.o "${INSTALL_DIR}/lib/"Scrt1.o \
      "${INSTALL_DIR}/lib/"crti.o "${INSTALL_DIR}/lib/"crtn.o "${OUT_DIR}/lib/"
cp -a "${INSTALL_DIR}/include/." "${OUT_DIR}/include/"

# pwndbg's version() reads musl's internal __libc_version (const char
# __libc_version[] in src/internal/version.c); the static test binaries pull it
# from libc.a via -Wl,-u. Fail loud if a future musl version ever drops it.
# Capture nm's output instead of piping it, so the check can't misfire on nm's
# exit status under `set -o pipefail`.
symbols=$(nm "${OUT_DIR}/lib/libc.a" 2>/dev/null) || true
case "${symbols}" in
    *__libc_version*) ;;
    *) echo "FATAL: __libc_version missing from libc.a for musl ${VERSION}"; exit 1 ;;
esac

# Cleanup build artifacts to save space
rm -rf "${SRC_DIR}" "${INSTALL_DIR}" "${TARBALL}"

echo "=== musl ${VERSION} built successfully ==="
ls -la "${OUT_DIR}/lib/"
