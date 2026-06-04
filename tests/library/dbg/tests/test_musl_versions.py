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
# exercised dynamically only -- there the loaded libc is identified by the
# "/tmp/tmpnam_XXXX" rodata string (the provider uses __freadahead only as a gate).
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

    if pwndbg.aglib.arch.name not in ("x86-64", "aarch64"):
        pytest.skip("musl version tests are x86-64/aarch64 only")

    assert pwndbg.libc.which() == pwndbg.libc.LibcType.MUSL
    assert pwndbg.libc.version() == musl_ver_tuple(musl_ver), (
        f"expected musl {musl_ver_tuple(musl_ver)}, detected {pwndbg.libc.version()}"
    )
