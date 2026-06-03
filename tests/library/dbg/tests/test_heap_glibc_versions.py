"""
Tests pwndbg heap commands against multiple glibc versions.

These tests use pre-built glibc shared libraries (downloaded by scripts/download-test-glibcs.sh)
and test binaries compiled against each version (built by Makefile targets).

Each test is parametrized by glibc version, verifying that pwndbg correctly handles
version-specific struct layouts, bin types, and behaviors.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from ....host import Controller
from . import get_binary
from . import launch_to
from . import pwndbg_test

# Versions parsed from Dockerfile.glibc-test-libs so the list lives in one place.
_DOCKERFILE = pathlib.Path(__file__).resolve().parents[4] / "Dockerfile.glibc-test-libs"
GLIBC_VERSIONS = re.findall(
    r"(?m)^FROM base-builder AS build-([0-9.]+)", _DOCKERFILE.read_text()
)
assert GLIBC_VERSIONS, f"no glibc versions parsed from {_DOCKERFILE}"


def glibc_ver_tuple(ver: str) -> tuple[int, int]:
    parts = ver.split(".")
    return (int(parts[0]), int(parts[1]))


# No-debug glibc binaries (built by the makefile against glibcs-nodebug/<ver>/). Present
# only when the heap-libc-tests workflow has built the no-debug image variant.
_NODEBUG_BINARIES = [
    (ver, get_binary(f"heap_malloc_chunk.glibc-{ver}-nodebug.out")) for ver in GLIBC_VERSIONS
]
_NODEBUG_BINARIES = [(ver, b) for ver, b in _NODEBUG_BINARIES if b.exists()]


@pytest.mark.parametrize("glibc_ver", GLIBC_VERSIONS)
@pwndbg_test
async def test_heap_version_detection(ctrl: Controller, glibc_ver: str) -> None:
    """Verify pwndbg correctly detects the glibc version from the loaded libc."""
    binary = get_binary(f"heap_malloc_chunk.glibc-{glibc_ver}.out")
    if not binary.exists():
        pytest.skip(f"glibc {glibc_ver} test binary not available")

    await ctrl.disable_debuginfod()
    await launch_to(ctrl, binary, "break_here")

    import pwndbg.aglib
    import pwndbg.libc

    if pwndbg.aglib.arch.name != "x86-64":
        pytest.skip("glibc version tests are x86-64 only")

    assert pwndbg.libc.which() == pwndbg.libc.LibcType.GLIBC
    detected = pwndbg.libc.version()
    expected = glibc_ver_tuple(glibc_ver)
    assert detected == expected, f"Expected glibc {expected}, detected {detected}"


@pytest.mark.parametrize("glibc_ver", GLIBC_VERSIONS)
@pwndbg_test
async def test_heap_allocator_setup(ctrl: Controller, glibc_ver: str) -> None:
    """Verify pwndbg sets up the correct heap allocator for each glibc version."""
    binary = get_binary(f"heap_malloc_chunk.glibc-{glibc_ver}.out")
    if not binary.exists():
        pytest.skip(f"glibc {glibc_ver} test binary not available")

    await ctrl.disable_debuginfod()
    await launch_to(ctrl, binary, "break_here")

    import pwndbg.aglib
    import pwndbg.aglib.heap
    from pwndbg.aglib.heap.ptmalloc import GlibcMemoryAllocator

    if pwndbg.aglib.arch.name != "x86-64":
        pytest.skip("glibc version tests are x86-64 only")

    allocator = pwndbg.aglib.heap.current
    assert isinstance(allocator, GlibcMemoryAllocator), (
        f"Expected GlibcMemoryAllocator, got {type(allocator)}"
    )

    # Verify tcache availability (present since 2.26)
    assert allocator.has_tcache, f"glibc {glibc_ver} should have tcache"

    assert allocator.main_arena is not None, f"main_arena should be found for glibc {glibc_ver}"

    assert allocator.mp is not None, f"mp (malloc_par) should be found for glibc {glibc_ver}"


@pytest.mark.parametrize("glibc_ver", GLIBC_VERSIONS)
@pwndbg_test
async def test_heap_bins_glibc_version(ctrl: Controller, glibc_ver: str) -> None:
    """Verify bin operations work correctly for each glibc version."""
    binary = get_binary(f"heap_bins.glibc-{glibc_ver}.out")
    if not binary.exists():
        pytest.skip(f"glibc {glibc_ver} test binary not available")

    import pwndbg.aglib
    import pwndbg.aglib.heap
    import pwndbg.aglib.memory
    import pwndbg.aglib.symbol
    from pwndbg.aglib.heap.ptmalloc import BinType
    from pwndbg.aglib.heap.ptmalloc import GlibcMemoryAllocator

    await ctrl.disable_debuginfod()
    await ctrl.launch(binary)

    if pwndbg.aglib.arch.name != "x86-64":
        pytest.skip("glibc version tests are x86-64 only")

    await ctrl.execute("set context-output /dev/null")
    await ctrl.execute("b breakpoint")
    await ctrl.cont()

    allocator = pwndbg.aglib.heap.current
    assert isinstance(allocator, GlibcMemoryAllocator)

    ver = glibc_ver_tuple(glibc_ver)

    # Read test parameters from the binary's exported symbols
    addr = pwndbg.aglib.symbol.lookup_symbol_addr("tcache_size")
    assert addr is not None
    tcache_size = allocator._request2size(pwndbg.aglib.memory.u64(addr))

    # After initial alloc_chunks(), verify tcache has one entry
    result = allocator.tcachebins()
    assert result is not None
    assert result.bin_type == BinType.TCACHE
    assert tcache_size in result.bins

    # Check fastbins (removed in glibc 2.43)
    addr = pwndbg.aglib.symbol.lookup_symbol_addr("fastbin_size")
    assert addr is not None
    fastbin_size = allocator._request2size(pwndbg.aglib.memory.u64(addr))

    result = allocator.fastbins()
    if ver >= (2, 43):
        # glibc 2.43 removed fastbins entirely - fastbins() returns None
        assert result is None, f"glibc {glibc_ver} should have no fastbins"
    else:
        assert result is not None
        assert result.bin_type == BinType.FAST
        assert fastbin_size in result.bins

    await ctrl.cont()

    result = allocator.tcachebins()
    assert result is not None
    assert tcache_size in result.bins

    await ctrl.cont()

    result = allocator.fastbins()
    if ver < (2, 43):
        assert result is not None

    await ctrl.cont()

    result = allocator.unsortedbin()
    assert result is not None
    assert result.bin_type == BinType.UNSORTED

    if ver >= (2, 42):
        # glibc 2.42+ has direct smallbin placement for some sizes, but may still
        # have entries in the unsorted bin (e.g. from coalescing or size thresholds)
        assert len(result.bins["all"].fd_chain) <= 1
    else:
        assert len(result.bins["all"].fd_chain) >= 1

    await ctrl.cont()

    result = allocator.smallbins()
    assert result is not None
    assert result.bin_type == BinType.SMALL

    await ctrl.cont()

    result = allocator.largebins()
    assert result is not None
    assert result.bin_type == BinType.LARGE

    # Run the 'bins' command to verify it doesn't crash
    await ctrl.execute("bins")


@pytest.mark.parametrize("glibc_ver", GLIBC_VERSIONS)
@pwndbg_test
async def test_heap_malloc_chunk_glibc_version(ctrl: Controller, glibc_ver: str) -> None:
    """Verify malloc_chunk command works for each glibc version."""
    binary = get_binary(f"heap_malloc_chunk.glibc-{glibc_ver}.out")
    if not binary.exists():
        pytest.skip(f"glibc {glibc_ver} test binary not available")

    import pwndbg.aglib
    import pwndbg.aglib.heap
    import pwndbg.aglib.symbol
    from pwndbg.aglib.heap.ptmalloc import GlibcMemoryAllocator

    await ctrl.disable_debuginfod()
    await launch_to(ctrl, binary, "break_here")

    if pwndbg.aglib.arch.name != "x86-64":
        pytest.skip("glibc version tests are x86-64 only")

    allocator = pwndbg.aglib.heap.current
    assert isinstance(allocator, GlibcMemoryAllocator)

    malloc_chunk = allocator.malloc_chunk
    assert malloc_chunk is not None, f"malloc_chunk type should be available for glibc {glibc_ver}"

    chunk_types = ["allocated_chunk", "tcache_chunk", "fast_chunk", "small_chunk", "large_chunk", "unsorted_chunk"]
    for name in chunk_types:
        addr = pwndbg.aglib.symbol.lookup_symbol_value(name)
        if addr is None or addr == 0:
            continue
        result = await ctrl.execute_and_capture(f"malloc-chunk {name}")
        assert len(result) > 0, f"malloc-chunk {name} produced no output for glibc {glibc_ver}"
        # Verify it shows chunk type info
        assert "chunk" in result.lower() or "Addr:" in result, (
            f"malloc-chunk {name} output doesn't look right for glibc {glibc_ver}: {result[:200]}"
        )


@pytest.mark.parametrize("glibc_ver", GLIBC_VERSIONS)
@pytest.mark.parametrize("use_heuristic", [False, True], ids=["debug-syms", "heuristic"])
@pwndbg_test
async def test_heap_heuristic_glibc_version(ctrl: Controller, glibc_ver: str, use_heuristic: bool) -> None:
    """Verify both debug-symbols and heuristic heap paths work for each glibc version."""
    binary = get_binary(f"heap_malloc_chunk.glibc-{glibc_ver}.out")
    if not binary.exists():
        pytest.skip(f"glibc {glibc_ver} test binary not available")

    import pwndbg.aglib
    import pwndbg.aglib.heap
    from pwndbg.aglib.heap.ptmalloc import GlibcMemoryAllocator

    await ctrl.disable_debuginfod()
    await ctrl.launch(binary)

    if pwndbg.aglib.arch.name != "x86-64":
        pytest.skip("glibc version tests are x86-64 only")

    if use_heuristic:
        await ctrl.execute("set resolve-heap-via-heuristic force")

    await ctrl.execute("b break_here")
    await ctrl.cont()

    allocator = pwndbg.aglib.heap.current
    assert isinstance(allocator, GlibcMemoryAllocator), (
        f"Expected GlibcMemoryAllocator for glibc {glibc_ver} "
        f"(heuristic={use_heuristic}), got {type(allocator)}"
    )

    main_arena = allocator.main_arena
    assert main_arena is not None, (
        f"main_arena not found for glibc {glibc_ver} (heuristic={use_heuristic})"
    )

    result = await ctrl.execute_and_capture("heap")
    assert len(result) > 0, f"'heap' command produced no output for glibc {glibc_ver}"


@pytest.mark.parametrize(
    "glibc_ver,binary",
    _NODEBUG_BINARIES,
    ids=[f"{ver}-nodebug" for ver, _ in _NODEBUG_BINARIES],
)
@pwndbg_test
async def test_heap_heuristic_nodebug_glibc_version(
    ctrl: Controller, glibc_ver: str, binary: pathlib.Path
) -> None:
    """Genuine no-debug-info path. The libc ships with no separate debug file and no
    .gnu_debuglink, so pwndbg cannot resolve main_arena by symbol: HeuristicHeap must
    scan .data/relocations to find the arena, and version() must fall back to the
    .rodata banner. (The 'heuristic' variant above only forces the code path while the
    libc symbols are still present, so it never exercises that scan.)"""
    import pwndbg.aglib
    import pwndbg.aglib.heap
    import pwndbg.libc
    from pwndbg.aglib.heap.ptmalloc import GlibcMemoryAllocator

    await ctrl.disable_debuginfod()
    await ctrl.launch(binary)

    if pwndbg.aglib.arch.name != "x86-64":
        pytest.skip("glibc version tests are x86-64 only")

    # Prove the libc symbols are genuinely gone; otherwise the heuristic would just read
    # main_arena by symbol and this test would be meaningless.
    assert pwndbg.libc.has_debug_info() is False

    await ctrl.execute("set resolve-heap-via-heuristic force")
    await ctrl.execute("b break_here")
    await ctrl.cont()

    allocator = pwndbg.aglib.heap.current
    assert isinstance(allocator, GlibcMemoryAllocator)
    # The heuristic .data/relocation scan must find main_arena with no symbols.
    assert allocator.main_arena is not None

    # version() must still resolve via the .rodata banner with no debug symbols.
    assert pwndbg.libc.version() == glibc_ver_tuple(glibc_ver)

    result = await ctrl.execute_and_capture("heap")
    assert len(result) > 0
