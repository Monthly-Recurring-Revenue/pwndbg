from __future__ import annotations

import functools
import os
import re
from collections.abc import Callable
from collections.abc import Coroutine
from inspect import signature
from pathlib import Path
from typing import Any
from typing import Concatenate
from typing import ParamSpec

from .... import host
from ....host import Controller

BINARIES_PATH = os.environ.get("TEST_BINARIES_ROOT", "/")

T = ParamSpec("T")


def pwndbg_test(
    test: Callable[Concatenate[Controller, T], Coroutine[Any, Any, None]],
) -> Callable[T, None]:
    @functools.wraps(test)
    def inner_test(*args: T.args, **kwargs: T.kwargs) -> None:
        async def _test(controller: Controller) -> None:
            await test(controller, *args, **kwargs)

        print(f"[+] Launching test {test.__name__} asynchronously")
        host.start(_test)

    # Remove the controller from the signature, as seen by Pytest.
    sig = signature(inner_test)
    sig = sig.replace(parameters=tuple(sig.parameters.values())[1:])
    inner_test.__signature__ = sig  # type: ignore[attr-defined]

    return inner_test


def get_binary(name: str) -> Path:
    return Path(BINARIES_PATH) / name


def glibc_test_versions() -> list[str]:
    """glibc versions with prebuilt artifacts, parsed from Dockerfile.glibc-test-libs
    (its build-<ver> stages) so the list lives in one place."""
    dockerfile = Path(__file__).resolve().parents[4] / "Dockerfile.glibc-test-libs"
    return re.findall(r"(?m)^FROM base-builder AS build-([0-9.]+)", dockerfile.read_text())


def glibc_version_binaries(stem: str) -> list[tuple[str, Path]]:
    """(id, binary) for the container's system build of `stem` plus each per-glibc
    -version build (`<stem>.glibc-<ver>.out`) present on disk. The per-version
    binaries exist only once the heap-libc-tests workflow has built them, so a
    normal run gets just the system one; this lets the same test run against every
    glibc version when they are available. `make all` fails loudly if a requested
    binary did not build, so filtering on existence cannot hide one."""
    targets = [("system", get_binary(f"{stem}.native.out"))]
    for ver in glibc_test_versions():
        targets.append((ver, get_binary(f"{stem}.glibc-{ver}.out")))
    return [(name, b) for name, b in targets if b.exists()]


def bionic_api_binaries() -> list[tuple[str, Path]]:
    """(id, binary) per Android API level with a prebuilt static bionic probe present.
    Parsed from Dockerfile.bionic-test-libs build-<API> stages. These ship prebuilt per
    API level (not compiled per version), so this is an API axis, not a version one."""
    dockerfile = Path(__file__).resolve().parents[4] / "Dockerfile.bionic-test-libs"
    apis = re.findall(r"(?m)^FROM base-builder AS build-([0-9]+)", dockerfile.read_text())
    targets = [
        (api, get_binary(f"bionics/{api}/bionic_probe.bionic-{api}-static.out")) for api in apis
    ]
    return [(name, b) for name, b in targets if b.exists()]


def break_at_sym(sym: str) -> None:
    import pwndbg
    from pwndbg.dbg_mod import BreakpointLocation

    inf = pwndbg.dbg.selected_inferior()
    addr = inf.lookup_symbol(sym)
    assert addr is not None
    inf.break_at(BreakpointLocation(int(addr)))


async def launch_to(ctrl: Controller, target: Path, sym: str) -> None:
    import pwndbg
    import pwndbg.aglib
    from pwndbg.dbg_mod import BreakpointLocation

    await ctrl.launch(target)

    inf = pwndbg.dbg.selected_inferior()
    addr = inf.lookup_symbol(sym)
    assert addr is not None
    if pwndbg.aglib.regs.pc != int(addr):
        inf.break_at(BreakpointLocation(int(addr)))
        await ctrl.cont()


def get_expr(expr: str):
    import pwndbg

    ctx = pwndbg.dbg.selected_frame() or pwndbg.dbg.selected_inferior()
    return ctx.evaluate_expression(expr)
