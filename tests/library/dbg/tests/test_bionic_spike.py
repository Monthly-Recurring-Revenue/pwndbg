"""
Bionic feasibility spike (throwaway).

Confirms that a fully-static Android-bionic x86_64 binary launches under gdb in
CI and reaches a user breakpoint -- the single open question that decides whether
the bionic deliverable can mirror the glibc/musl harness (static, no emulator).
It also reports what pwndbg currently detects, which is expected to be UNKNOWN
until a bionic provider (the Android-Debugging project's deliverable) exists.
"""

from __future__ import annotations

import pytest

from ....host import Controller
from . import get_binary
from . import launch_to
from . import pwndbg_test

BINARY = get_binary("bionic_probe.bionic-21-static.out")


@pwndbg_test
async def test_bionic_static_runs(ctrl: Controller) -> None:
    if not BINARY.exists():
        pytest.skip("bionic probe binary not available")

    import pwndbg.aglib
    import pwndbg.libc

    await ctrl.disable_debuginfod()
    # Reaching break_here at all is the spike's real result: it proves the static
    # bionic binary started and ran under gdb on plain Linux, no emulator.
    await launch_to(ctrl, BINARY, "break_here")

    if pwndbg.aglib.arch.name != "x86-64":
        pytest.skip("bionic spike is x86-64 only")

    # Spike telemetry (not yet an assertion): what does pwndbg make of bionic today?
    print(f"[BIONIC SPIKE] reached break_here; pwndbg.libc.which()={pwndbg.libc.which()}")

    assert pwndbg.aglib.regs.pc != 0
