"""
Tests pwndbg's musl detection against multiple musl versions.

Uses test binaries statically linked against pre-built musl libcs (produced by
Dockerfile.musl-test-libs and extracted into tests/binaries/host/musls/). The musl provider
(pwndbg/libc/musl.py) already exists; these tests feed it per-version binaries and
assert it detects the right version -- something the existing musl tests do not do.
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


def musl_ver_tuple(ver: str) -> tuple[int, ...]:
    return tuple(int(p) for p in ver.split("."))


@pytest.mark.parametrize("musl_ver", MUSL_VERSIONS)
@pwndbg_test
async def test_musl_version_detection(ctrl: Controller, musl_ver: str) -> None:
    """pwndbg detects musl and resolves the exact version from a binary statically
    linked against that musl version."""
    binary = get_binary(f"heap_musl.musl-{musl_ver}-static.out")
    if not binary.exists():
        pytest.skip(f"musl {musl_ver} test binary not available")

    import pwndbg.aglib
    import pwndbg.libc

    await ctrl.disable_debuginfod()
    await launch_to(ctrl, binary, "break_here")

    if pwndbg.aglib.arch.name != "x86-64":
        pytest.skip("musl version tests are x86-64 only")

    assert pwndbg.libc.which() == pwndbg.libc.LibcType.MUSL
    assert pwndbg.libc.version() == musl_ver_tuple(musl_ver), (
        f"expected musl {musl_ver_tuple(musl_ver)}, detected {pwndbg.libc.version()}"
    )
