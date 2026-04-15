from __future__ import annotations

import pytest

from ....host import Controller
from . import get_binary
from . import launch_to
from . import pwndbg_test

HEAP_VIS = get_binary("heap_vis.native.out")


@pwndbg_test
async def test_vis_heap_chunk_command(ctrl: Controller) -> None:
    import pwndbg.aglib
    import pwndbg.aglib.memory
    import pwndbg.aglib.vmmap

    # Disable collapsible output for existing test expectations
    await ctrl.execute("set vis-skip-repeating-val off")

    await launch_to(ctrl, HEAP_VIS, "break_here")

    if pwndbg.aglib.arch.name != "x86-64":
        pytest.skip("TODO multiarch")

    # TODO/FIXME: Shall we have a standard method to do this kind of filtering?
    # Note that we have `pages_filter` in pwndbg/pwndbg/commands/vmmap.py heh
    heap_page = next(page for page in pwndbg.aglib.vmmap.get() if page.objfile == "[heap]")

    first_chunk_size = pwndbg.aglib.memory.u64(heap_page.start + pwndbg.aglib.arch.ptrsize)

    # Just a sanity check...
    assert (heap_page.start & 0xFFF) == 0

    result = (await ctrl.execute_and_capture("vis-heap-chunk 1")).splitlines()

    # We will use `heap_addr` variable to fill in proper addresses below
    heap_addr = heap_page.start

    # We sometimes need that value, so let's cache it
    dq2 = None

    def heap_iter(offset=0x10):
        nonlocal heap_addr
        heap_addr += offset
        return heap_addr

    async def hexdump_16B(gdb_symbol):
        from pwndbg.commands.ptmalloc2 import bin_ascii

        first, second = (await ctrl.execute_and_capture(f"x/16xb {gdb_symbol}")).splitlines()
        first = [int(v, 16) for v in first.split(":")[1].split()]
        second = [int(v, 16) for v in second.split(":")[1].split()]

        return bin_ascii(first + second)

    async def hexdump_8B(addr_val):
        from pwndbg.commands.ptmalloc2 import bin_ascii

        raw = (await ctrl.execute_and_capture(f"x/8xb {addr_val}")).splitlines()
        return bin_ascii([int(v, 16) for v in raw[0].split(":")[1].split()])

    async def vis_heap_line(heap_iter_offset=0x10, suffix=""):
        """Returns data to format a vis_heap_chunk line"""
        addr = heap_iter(heap_iter_offset)
        hexdump = await hexdump_16B(addr)

        nonlocal dq2
        dq1, dq2 = map(pwndbg.aglib.memory.u64, (addr, addr + 8))

        formatted = f"{addr:#x}\t{dq1:#018x}\t{dq2:#018x}\t{hexdump}"
        formatted += suffix

        return formatted

    first_hexdump = await hexdump_16B(hex(heap_page.start))

    # Build expected output for the first chunk by reading actual memory.
    # The first chunk may be a tcache struct (pre-2.43) or a regular user allocation (2.43+).
    expected = [
        f"{heap_iter(0):#x}\t0x0000000000000000\t{first_chunk_size | 1:#018x}\t{first_hexdump}",
    ]

    # First chunk body: (first_chunk_size // 16 - 1) full 16-byte lines + 1 half-line (8 bytes)
    for _ in range(first_chunk_size // 16 - 1):
        expected.append(await vis_heap_line())

    # Last line of first chunk: only first qword shown (second qword is next chunk's prev_size)
    last_addr = heap_iter()
    last_dq1 = pwndbg.aglib.memory.u64(last_addr)
    dq2 = last_dq1
    last_half_hex = await hexdump_8B(last_addr)
    expected.append(f"{last_addr:#x}\t{last_dq1:#018x}\t                  \t{last_half_hex}")

    assert result == expected

    ## This time using `default-visualize-chunk-number` to set `count`, to make sure that the config can work
    await ctrl.execute("set default-visualize-chunk-number 1")
    assert pwndbg.config.default_visualize_chunk_number == 1
    result = (await ctrl.execute_and_capture("vis-heap-chunk")).splitlines()
    # No parameters were passed and top isn't reached so help text is shown
    no_params_help = "Not all chunks were shown, see `vis --help` for more information."
    assert result == expected + [no_params_help]
    await ctrl.execute(
        f"set default-visualize-chunk-number {pwndbg.config.default_visualize_chunk_number.default}"
    )

    ## Test vis_heap_chunk with increasing counts
    # Instead of hardcoding chunk sizes/content, verify that:
    # - Each count shows progressively more lines
    # - The final count (showing all chunks) includes the Top chunk marker
    result1 = result
    del result
    del expected

    result2 = (await ctrl.execute_and_capture("vis-heap-chunk 2")).splitlines()
    assert len(result2) > len(result1)
    # result2 should start the same as result1 (minus the last half-line)
    assert result2[: len(result1) - 1] == result1[:-1]

    result3 = (await ctrl.execute_and_capture("vis-heap-chunk 3")).splitlines()
    assert len(result3) >= len(result2)

    # Show all chunks - should include Top chunk marker
    result_all = (await ctrl.execute_and_capture("vis-heap-chunk")).splitlines()
    assert any("<-- Top chunk" in line for line in result_all)

    del result1
    del result2
    del result3
    del result_all

    # Continue, so that another allocation is made
    await ctrl.cont()

    # After a new allocation, vis should show more data
    result_after_alloc = (await ctrl.execute_and_capture("vis-heap-chunk")).splitlines()
    assert any("<-- Top chunk" in line for line in result_after_alloc)

    del result_after_alloc

    ## Continue, so that alloc[1] is freed
    await ctrl.cont()

    result_all3 = (await ctrl.execute_and_capture("vis-heap-chunk")).splitlines()

    # After freeing a chunk, the output should contain tcache annotation and Top chunk
    assert any("tcachebins" in line for line in result_all3)
    assert any("<-- Top chunk" in line for line in result_all3)

    del result_all3

    # Continue, malloc two large chunks and free one
    await ctrl.cont()

    # Get default result without max-visualize-chunk-size setting
    default_result = (await ctrl.execute_and_capture("vis-heap-chunk")).splitlines()
    assert len(default_result) > 10  # should have some chunks

    # Set max display size to 100 (no "0x" for misalignment)
    await ctrl.execute("set max-visualize-chunk-size 100")

    omitted_result = (await ctrl.execute_and_capture("vis-heap-chunk")).splitlines()
    assert len(omitted_result) < 0x30
    for omitted_line in omitted_result:
        assert omitted_line in default_result or set(omitted_line) == {"."}

    no_truncate_result = (await ctrl.execute_and_capture("vis-heap-chunk -n")).splitlines()
    assert no_truncate_result == default_result

    del default_result
    del omitted_result
    del no_truncate_result

    # Continue, mock overflow changing the chunk size
    await ctrl.cont()

    overflow_result = await ctrl.execute_and_capture("vis-heap-chunk")
    assert "\t0x0000000000000000\t0x4141414141414141\t........AAAAAAAA" in overflow_result
    assert len(overflow_result.splitlines()) < 0x500

    del overflow_result

    ## Test vis-skip-repeating-val config (collapsible output)
    # Test collapsible output on default_result which has many repeated lines
    # First ensure it's disabled (already should be from test start)
    await ctrl.execute("set vis-skip-repeating-val off")
    full_result_no_collapse = (await ctrl.execute_and_capture("vis-heap-chunk")).splitlines()

    # Should NOT contain collapse messages
    collapse_lines_disabled = [
        line for line in full_result_no_collapse if "repeated lines skipped" in line
    ]
    assert len(collapse_lines_disabled) == 0, (
        "Should have no collapse messages when skip-repeating is disabled"
    )

    # Now test with skip-repeating enabled on the same state
    await ctrl.execute("set vis-skip-repeating-val on")
    collapsed_result = (await ctrl.execute_and_capture("vis-heap-chunk")).splitlines()

    # Should contain collapse messages
    collapse_lines = [line for line in collapsed_result if "repeated lines skipped" in line]
    assert len(collapse_lines) > 0, "Should have collapse messages when skip-repeating is enabled"

    # Verify format of collapse message (should have tab prefix and right-aligned count)
    for collapse_line in collapse_lines:
        assert collapse_line.strip().startswith("... ↓"), (
            "Collapse message should start with '... ↓'"
        )
        assert "repeated lines skipped" in collapse_line, "Should say 'repeated lines skipped'"

    # Full result should have more lines than collapsed result
    assert len(full_result_no_collapse) > len(collapsed_result), (
        "Full output should have more lines than collapsed output"
    )

    # Set back to off for any remaining tests
    await ctrl.execute("set vis-skip-repeating-val off")

    del collapsed_result
    del full_result_no_collapse
