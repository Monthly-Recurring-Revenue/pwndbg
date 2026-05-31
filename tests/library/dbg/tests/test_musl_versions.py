"""
Tests pwndbg's musl detection against multiple musl versions.

Uses test binaries linked against pre-built musl libcs (produced by
Dockerfile.musl-test-libs and extracted into tests/binaries/host/musls/), both
statically and dynamically. The musl provider (pwndbg/libc/musl.py) already
exists; these tests feed it per-version binaries and assert it detects the right
version -- something the existing musl tests do not do.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from ....host import Controller
from . import get_binary
from . import launch_to
from . import pwndbg_test

# Versions parsed from Dockerfile.musl-test-libs (its build-<ver> stages) so the
# list lives in one place.
_DOCKERFILE = pathlib.Path(__file__).resolve().parents[4] / "Dockerfile.musl-test-libs"
MUSL_VERSIONS = re.findall(r"(?m)^FROM base-builder AS build-([0-9.]+)", _DOCKERFILE.read_text())
assert MUSL_VERSIONS, f"no musl versions parsed from {_DOCKERFILE}"

# mallocng replaced musl's old allocator in 1.2.1. A statically-linked binary is
# only fingerprintable as musl via the mallocng signature, so older versions are
# exercised dynamically only (where the exported __freadahead drives detection).
_MALLOCNG_MIN = (1, 2, 1)


def musl_ver_tuple(ver: str) -> tuple[int, ...]:
    return tuple(int(p) for p in ver.split("."))


def _version_linkages() -> list[tuple[str, str]]:
    combos: list[tuple[str, str]] = []
    for ver in MUSL_VERSIONS:
        combos.append((ver, "dynamic"))
        if musl_ver_tuple(ver) >= _MALLOCNG_MIN:
            combos.append((ver, "static"))
    return combos


_COMBOS = _version_linkages()


@pytest.mark.parametrize(
    "musl_ver,linkage", _COMBOS, ids=[f"{ver}-{linkage}" for ver, linkage in _COMBOS]
)
@pwndbg_test
async def test_musl_version_detection(ctrl: Controller, musl_ver: str, linkage: str) -> None:
    """pwndbg detects musl and resolves the exact version from a binary linked
    against that musl version."""
    binary = get_binary(f"heap_musl.musl-{musl_ver}-{linkage}.out")
    if not binary.exists():
        pytest.skip(f"musl {musl_ver} ({linkage}) test binary not available")

    import pwndbg.aglib
    import pwndbg.libc

    await ctrl.disable_debuginfod()
    await launch_to(ctrl, binary, "break_here")

    if pwndbg.aglib.arch.name != "x86-64":
        pytest.skip("musl version tests are x86-64 only")

    # --- TEMP DIAGNOSTIC (remove after): is the dynamic binary actually a proper
    # dynamic ELF? Dump ELF type / PT_INTERP / DT_NEEDED + what gdb sees. ---
    if linkage == "dynamic":
        import subprocess

        import pwndbg.libc.facade as _facade

        for _a in (["readelf", "-hW"], ["readelf", "-lW"], ["readelf", "-dW"]):
            _r = subprocess.run([*_a, str(binary)], capture_output=True, text=True)
            print(f"[DIAG {musl_ver}] $ {' '.join(_a)} <bin>\n{_r.stdout}{_r.stderr}")
        print(f"[DIAG {musl_ver}] info sharedlibrary:\n{await ctrl.execute_and_capture('info sharedlibrary')}")
        try:
            print(f"[DIAG {musl_ver}] facade.filepath={_facade.filepath()}")
        except Exception as _e:  # noqa: BLE001
            print(f"[DIAG {musl_ver}] facade.filepath error: {_e}")
    # --- END TEMP DIAGNOSTIC ---

    assert pwndbg.libc.which() == pwndbg.libc.LibcType.MUSL
    assert pwndbg.libc.version() == musl_ver_tuple(musl_ver), (
        f"expected musl {musl_ver_tuple(musl_ver)}, detected {pwndbg.libc.version()}"
    )
