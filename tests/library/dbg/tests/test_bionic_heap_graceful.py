from __future__ import annotations

from pathlib import Path

import pytest

from ....host import Controller
from . import bionic_api_binaries
from . import launch_to
from . import pwndbg_test

# A static bionic binary uses Android's scudo allocator, which pwndbg does not support
# (and there is no bionic libc provider). The point of these tests is graceful
# degradation: a heap command may error on such a binary, but it must not hang or wedge
# the session. We run the heap commands tolerating errors, then assert pwndbg is still
# responsive afterwards, never asserting any particular error wording.
_BIONIC_BINARIES = bionic_api_binaries()


@pytest.mark.parametrize(
    "binary", [b for _, b in _BIONIC_BINARIES], ids=[i for i, _ in _BIONIC_BINARIES]
)
@pwndbg_test
async def test_bionic_heap_commands_degrade_gracefully(ctrl: Controller, binary: Path) -> None:
    import pwndbg.aglib

    await ctrl.disable_debuginfod()
    await launch_to(ctrl, binary, "break_here")

    if pwndbg.aglib.arch.name != "x86-64":
        pytest.skip("bionic tests are x86-64 only")

    # The glibc ptmalloc provider cannot resolve on bionic and bionic uses scudo (not
    # jemalloc), so these may raise out of the command layer rather than degrade
    # silently. That is acceptable; tolerate it and check the session survives.
    for cmd in ("heap", "bins", "jemalloc heap"):
        try:
            await ctrl.execute_and_capture(cmd)
        except Exception:
            pass

    # pwndbg must remain responsive after the (possibly failing) heap commands.
    vmmap = await ctrl.execute_and_capture("vmmap")
    assert "0x" in vmmap, vmmap
