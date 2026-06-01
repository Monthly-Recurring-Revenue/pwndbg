"""
Tests the bionic (Android libc) multi-API test harness.

Uses static Android-bionic test binaries pre-built by Dockerfile.bionic-test-libs
(one per API level) and extracted into tests/binaries/host/bionics/. Each binary
is fully static, so it launches under gdb on plain Linux with no emulator.

For each API level this asserts the binary RUNS under gdb (reaches break_here)
and that the build-target API level encoded in its .note.android.ident ELF note
(the android_api field) matches the expected API.

NOTE: this does NOT assert pwndbg.libc.which() == BIONIC. There is no bionic
provider yet -- detection-as-BIONIC is deferred to the separate Android-Debugging
project's deliverable. This test validates the build/run/version axis that the
harness itself owns, mirroring how test_musl_versions.py owns the musl version
axis.
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

    detected_api = _read_android_api(binary)
    assert detected_api == api, (
        f"expected android_api {api}, got {detected_api} from .note.android.ident"
    )
