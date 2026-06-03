"""
Tests the bionic (Android libc) multi-API test harness.

Uses static Android-bionic test binaries pre-built by Dockerfile.bionic-test-libs
(one per API level) and extracted into tests/binaries/host/bionics/. Each binary
is fully static, so it launches under gdb on plain Linux with no emulator.

For each API level this asserts the static binary RUNS under gdb (reaches
break_here) and that the android_api in its .note.android.ident ELF note matches
the expected API.

The note read here is a build/run check (read from the on-disk file). pwndbg's own
detection on a bionic binary is covered by test_bionic_libc_detection below: which()
== UNKNOWN (there is no bionic provider yet -- a real BIONIC type is the separate
Android-Debugging project's deliverable), version() == (-1, -1), and has_debug_info()
is False. test_bionic_commands and test_bionic_heap_graceful cover that pwndbg's
libc-agnostic commands work and its heap commands degrade gracefully on a bionic
(scudo) binary.
"""

from __future__ import annotations

import pathlib
import re
import struct

import pytest

from ....host import Controller
from . import get_binary
from . import launch_to
from . import pwndbg_test

# API levels parsed from Dockerfile.bionic-test-libs (its build-<API> stages) so
# the list lives in one place. API levels are plain integers.
_DOCKERFILE = pathlib.Path(__file__).resolve().parents[4] / "Dockerfile.bionic-test-libs"
_API_STRINGS = re.findall(r"(?m)^FROM base-builder AS build-([0-9]+)", _DOCKERFILE.read_text())
BIONIC_APIS = [int(a) for a in _API_STRINGS]
assert BIONIC_APIS, f"no API levels parsed from {_DOCKERFILE}"


def _read_android_api(binary: pathlib.Path) -> int:
    """Read the android_api field from a binary's .note.android.ident ELF note.

    The note's descriptor begins with a 4-byte little-endian android_api value
    (the build-target API level), followed by NDK build/version strings. We read
    the section's raw bytes and take the first word, which is robust across
    pyelftools versions (it does not special-case this Android note).
    """
    from elftools.elf.elffile import ELFFile

    with open(binary, "rb") as f:
        elf = ELFFile(f)
        section = elf.get_section_by_name(".note.android.ident")
        assert section is not None, f".note.android.ident missing from {binary}"
        data = section.data()

    # ELF note layout: n_namesz, n_descsz, n_type (4 bytes LE each), then the
    # name padded to 4 bytes, then the descriptor. The descriptor's first word
    # is android_api.
    n_namesz, _n_descsz, _n_type = struct.unpack_from("<III", data, 0)
    desc_off = 12 + ((n_namesz + 3) & ~3)
    (android_api,) = struct.unpack_from("<I", data, desc_off)
    return android_api


@pytest.mark.parametrize("api", BIONIC_APIS, ids=[str(a) for a in BIONIC_APIS])
@pwndbg_test
async def test_bionic_version(ctrl: Controller, api: int) -> None:
    """The static bionic binary for `api` runs under gdb and its
    .note.android.ident android_api field equals `api`."""
    binary = get_binary(f"bionics/{api}/bionic_probe.bionic-{api}-static.out")
    if not binary.exists():
        pytest.skip(f"bionic API {api} test binary not available")

    import pwndbg.aglib

    await ctrl.disable_debuginfod()
    # Reaching break_here proves the static bionic binary started and ran under
    # gdb on plain Linux, no emulator.
    await launch_to(ctrl, binary, "break_here")

    if pwndbg.aglib.arch.name != "x86-64":
        pytest.skip("bionic version tests are x86-64 only")

    note_api = _read_android_api(binary)
    assert note_api == api, (
        f"expected android_api {api}, got {note_api} from .note.android.ident"
    )


@pytest.mark.parametrize("api", BIONIC_APIS, ids=[str(a) for a in BIONIC_APIS])
@pwndbg_test
async def test_bionic_libc_detection(ctrl: Controller, api: int) -> None:
    """What pwndbg's libc detection does on a static bionic binary today: there is no
    bionic provider, so which() is UNKNOWN (and must not crash), version() is (-1, -1),
    and has_debug_info() is False. Turning these into a real BIONIC assertion is the
    separate Android-Debugging project's job."""
    binary = get_binary(f"bionics/{api}/bionic_probe.bionic-{api}-static.out")
    if not binary.exists():
        pytest.skip(f"bionic API {api} test binary not available")

    import pwndbg.aglib
    import pwndbg.libc

    await ctrl.disable_debuginfod()
    await launch_to(ctrl, binary, "break_here")

    if pwndbg.aglib.arch.name != "x86-64":
        pytest.skip("bionic tests are x86-64 only")

    assert pwndbg.libc.which() == pwndbg.libc.LibcType.UNKNOWN
    assert pwndbg.libc.version() == (-1, -1)
    assert pwndbg.libc.has_debug_info() is False
