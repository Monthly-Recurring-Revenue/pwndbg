from __future__ import annotations

from pathlib import Path

import pytest

from ....host import Controller
from . import bionic_api_binaries
from . import launch_to
from . import pwndbg_test

# pwndbg's libc-agnostic commands should work on ANY static x86-64 binary, including
# a static Android (bionic) one, even though pwndbg has no bionic libc provider. These
# assert the commands run and produce plausible output (addresses), never an exact
# layout - bionic's heap/struct layout differs and is not what we are checking here.
_BIONIC_BINARIES = bionic_api_binaries()


@pytest.mark.parametrize(
    "binary", [b for _, b in _BIONIC_BINARIES], ids=[i for i, _ in _BIONIC_BINARIES]
)
@pwndbg_test
async def test_bionic_libc_agnostic_commands(ctrl: Controller, binary: Path) -> None:
    import pwndbg.aglib

    await ctrl.disable_debuginfod()
    await launch_to(ctrl, binary, "break_here")

    if pwndbg.aglib.arch.name != "x86-64":
        pytest.skip("bionic tests are x86-64 only")

    # vmmap reads the process memory map (no libc dependency).
    vmmap = await ctrl.execute_and_capture("vmmap")
    assert "0x" in vmmap, vmmap

    # Disassembly around the program counter.
    nearpc = await ctrl.execute_and_capture("nearpc")
    assert "0x" in nearpc, nearpc

    # Telescope dereferences the stack pointer.
    tele = await ctrl.execute_and_capture("telescope $sp")
    assert "0x" in tele, tele

    # Backtrace walks return addresses; break_here is a real frame.
    bt = await ctrl.execute_and_capture("backtrace")
    assert "break_here" in bt or "0x" in bt, bt
