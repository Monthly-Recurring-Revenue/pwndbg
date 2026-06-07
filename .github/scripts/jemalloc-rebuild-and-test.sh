#!/usr/bin/env bash
# Recompile jemalloc inside the prebuilt pwndbg image, then run only the jemalloc
# tests. Driven by .github/workflows/jemalloc-flaky-check.yml -- see that file for
# why we rebuild jemalloc instead of using the image's prebuilt copy.
#
# Env:
#   JEMALLOC_URL, JEMALLOC_SHA256 - pinned jemalloc source tarball + checksum
#   LG_VADDR_FORCE - empty to use this runner's native geometry (the roulette),
#                    or a number to force ./configure --with-lg-vaddr=<n>
set -euo pipefail

dump() {
  echo "---- $1 ----"
  cat "$2" 2>/dev/null || true
}

cd /tmp
curl -fsSL --retry 3 -o jemalloc.tar.bz2 "$JEMALLOC_URL"
echo "${JEMALLOC_SHA256}  jemalloc.tar.bz2" | sha256sum -c -
rm -rf jemalloc-5.3.0
tar xf jemalloc.tar.bz2
cd jemalloc-5.3.0

cfg_args=()
if [ -n "${LG_VADDR_FORCE:-}" ]; then
  cfg_args+=("--with-lg-vaddr=${LG_VADDR_FORCE}")
fi
# Installs over the image's prebuilt jemalloc at /usr/local; pkg-config (and thus
# the test Makefile) then links this freshly built copy.
./configure --prefix=/usr/local "${cfg_args[@]}" >/tmp/je-configure.log 2>&1 \
  || { dump "configure" /tmp/je-configure.log; exit 1; }
make -j"$(nproc)" >/tmp/je-make.log 2>&1 || { dump "make" /tmp/je-make.log; exit 1; }
make install >/tmp/je-install.log 2>&1 || { dump "make install" /tmp/je-install.log; exit 1; }
ldconfig || true

va="$(grep -iE 'significant virtual address bits' /tmp/je-configure.log | grep -oE '[0-9]+' | tail -1 || true)"
label="LG_VADDR=${va:-${LG_VADDR_FORCE:-?}}${LG_VADDR_FORCE:+ (forced)}"
echo "::notice title=jemalloc geometry::built with ${label}"
echo "=== jemalloc built with ${label} ==="

cd /pwndbg
# Rebuild the jemalloc test binaries against the just-installed jemalloc. Remove
# any stale artifacts first so they are guaranteed to relink (a fresh CI checkout
# has none; this just keeps the script correct if run on a dirty tree too).
rm -f tests/binaries/host/heap_jemalloc_extent_info.native.out \
  tests/binaries/host/heap_jemalloc_heap.native.out
make -C tests/binaries/host -j4 \
  heap_jemalloc_extent_info.native.out heap_jemalloc_heap.native.out
gdb --batch # one-time libpython symlink (matches the Test Docker workflow)
echo "=== rtree geometry baked into the test binary ==="
gdb --batch -nx -ex "print rtree_levels" \
  tests/binaries/host/heap_jemalloc_extent_info.native.out 2>/dev/null | grep -i bits || true

# Run only the jemalloc tests and require all three to pass -- guards against a
# rename silently collecting zero tests and still reporting green.
./tests.sh -d gdb -g dbg test_jemalloc 2>&1 | tee /tmp/jemalloc-test.log
grep -q "Tests Passed : 3" /tmp/jemalloc-test.log \
  || { echo "::error::expected 3 jemalloc tests to pass"; exit 1; }
