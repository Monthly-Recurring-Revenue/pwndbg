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
    # On 2.43, GDB output has multiple repeats groups (e.g. {0x8, 0x10 <repeats 60 times>, 0x0, ...})
    exp_regex = (
        "struct tcache_perthread_struct @ 0x[0-9a-f]+\n"
        "    0x[0-9a-f]+ \\+0x0000 (counts|num_slots) +: +.*\\{.+\\}\n"
        "    0x[0-9a-f]+ \\+0x[0-9a-f]{4} entries +: +.*\\{.+\\}"
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
