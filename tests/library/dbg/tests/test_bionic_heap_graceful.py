from __future__ import annotations

from pathlib import Path

import pytest

from ....host import Controller
from . import bionic_api_binaries
from . import launch_to
from . import pwndbg_test

# A static bionic binary uses Android's scudo allocator, which pwndbg does not support
# (and there is no bionic libc provider). The point of these tests is graceful
# degradation: heap commands must RETURN (print an error or nothing) rather than crash
# or hang, and pwndbg must stay responsive afterwards. We assert the commands return
# and a benign command still works - never any particular error wording.
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

    # The glibc ptmalloc provider cannot resolve on bionic, and bionic uses scudo (not
    # jemalloc), so each of these must return gracefully rather than raise out of the
    # command layer.
    for cmd in ("heap", "bins", "jemalloc heap"):
        out = await ctrl.execute_and_capture(cmd)
        assert isinstance(out, str), cmd

    # pwndbg must remain responsive after the failed heap commands.
    vmmap = await ctrl.execute_and_capture("vmmap")
    assert "0x" in vmmap, vmmap
