from __future__ import annotations

import re

import pytest

from ....host import Controller
from . import get_binary
from . import launch_to
from . import pwndbg_test

HEAP_MALLOC_CHUNK = get_binary("heap_malloc_chunk.native.out")


@pwndbg_test
async def test_command_dt_works_with_address(ctrl: Controller) -> None:
    import pwndbg.aglib

    await launch_to(ctrl, HEAP_MALLOC_CHUNK, "break_here")

    if pwndbg.aglib.arch.name != "x86-64":
        pytest.skip("TODO multiarch")

    tcache = await ctrl.execute_and_capture("print tcache")

    tcache_addr = tcache.split()[-1]

    out = await ctrl.execute_and_capture(f'dt "struct tcache_perthread_struct" {tcache_addr}')

    # Accounting for differences between architectures and glibc versions (2.42+, 2.43+)
    # GDB compact: {0x8, 0x10 <repeats 60 times>, 0x0, 0x10 <repeats 14 times>}
    # LLDB multiline: {\n  [0] = 8\n  [1] = 16\n  ...\n}
    # counts/num_slots field: values are integers (decimal or hex)
    # entries field: values are pointers (hex) or NULL
    gdb_int_vals = r"(0x[0-9a-f]+|[0-9]+)(, (0x[0-9a-f]+|[0-9]+)( <repeats [0-9]+ times>)?)*"
    lldb_int_vals = r"(\s*(\[[0-9]+\] = [0-9]+|\.\.\.)\n?)+"
    gdb_ptr_vals = r"(0x[0-9a-f]+|NULL)(, (0x[0-9a-f]+|NULL)( <repeats [0-9]+ times>)?)*"
    lldb_ptr_vals = r"(\s*(\[[0-9]+\] = (0x[0-9a-f]+|NULL)|\.\.\.)\n?)+"
    exp_regex = (
        r"struct tcache_perthread_struct @ 0x[0-9a-f]+"
        rf"\n    0x[0-9a-f]+ \+0x0000 (counts|num_slots) +: +.*\{{({gdb_int_vals}|{lldb_int_vals})\s*\}}"
        rf"\n    0x[0-9a-f]+ \+0x[0-9a-f]{{4}} entries +: +.*\{{({gdb_ptr_vals}|{lldb_ptr_vals})\s*\}}"
    )
    assert re.match(exp_regex, out)


@pwndbg_test
async def test_command_dt_works_with_no_address(ctrl: Controller) -> None:
    import pwndbg.aglib

    await launch_to(ctrl, HEAP_MALLOC_CHUNK, "break_here")

    if pwndbg.aglib.arch.name != "x86-64":
        pytest.skip("TODO multiarch")

    out = await ctrl.execute_and_capture('dt "struct tcache_perthread_struct"')

    exp_regex = (
        "struct tcache_perthread_struct\n"
        "    \\+0x0000 (counts|num_slots) +: +uint16_t ?\\[(64|76)\\]\n"
        "    \\+0x[0-9a-f]{4} entries +: +tcache_entry ?\\*\\[(64|76)\\]\n"
    )
    assert re.match(exp_regex, out)
