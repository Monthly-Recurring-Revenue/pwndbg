from __future__ import annotations

import atexit
import os
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

from ..library.qemu_system import images as kernel_images
from ..library.qemu_system import qemu
from . import TestResult
from . import TestStatus
from .gdb import GDBTestHost

_BOOT_TIMEOUT_S = 180

# every live VM is driven by a GDB session that loads the full vmlinux, so the
# memory cost is dominated by GDB, not QEMU; only ~cpu_count tests run at once
_MAX_VMS = min(os.cpu_count() or 1, 4)


def _console_tail(log: Path, lines: int = 40) -> str:
    # the log path alone is useless on CI, where the runner is gone by the
    # time anyone reads the failure
    try:
        return "\n".join(log.read_text(errors="replace").splitlines()[-lines:])
    except OSError as e:
        return f"<could not read console log: {e}>"


def _warn_if_ptrace_restricted() -> None:
    """Pwndbg's kernel-vmmap reads guest memory through /proc/<qemu-pid>/mem
    (QemuMachine in pwndbg/aglib/kernel/vmmap.py), which Yama's default
    ptrace_scope=1 forbids for non-child processes unless we are root.
    """
    try:
        scope = int(Path("/proc/sys/kernel/yama/ptrace_scope").read_text())
    except (OSError, ValueError):
        return

    if scope != 0 and os.geteuid() != 0:
        print(
            "WARNING: You are not running as root and ptrace_scope is not set to zero.\n"
            "Some tests will fail to read guest memory through /proc/<qemu-pid>/mem.\n"
            "To fix this, run the following command:\n"
            "\n"
            "    echo 0 | sudo tee /proc/sys/kernel/yama/ptrace_scope\n"
        )


class _VmManager:
    """Boots and reuses VMs across configurations under a single global limit.

    A test acquires a VM for its configuration and releases it when done. VMs
    are reused within a configuration; to stay under the limit, an idle VM from
    another configuration is evicted before a new one boots, so VMs never
    accumulate as the run moves from one configuration to the next.
    """

    def __init__(self, boot: Callable[[qemu.KernelConfig], qemu.QemuVM], limit: int):
        self._boot = boot
        self._limit = limit
        self._cond = threading.Condition()
        # everything below is protected by _cond:
        self._idle: dict[str, list[qemu.QemuVM]] = {}
        self._live = 0
        self._closed = False
        # configs whose first boot failed; cached so their remaining tests fail
        # fast instead of each re-attempting the same broken boot
        self._boot_error: dict[str, str] = {}

    def acquire(self, config: qemu.KernelConfig) -> qemu.QemuVM:
        with self._cond:
            while True:
                if self._closed:
                    raise RuntimeError("VM manager is shut down")
                if config.id in self._boot_error:
                    raise RuntimeError(self._boot_error[config.id])

                # reuse an idle VM of this config, discarding any that died
                # while idle
                idle = self._idle.get(config.id)
                while idle:
                    vm = idle.pop()
                    if vm.alive():
                        return vm
                    self._live -= 1

                if self._live < self._limit or self._evict_idle():
                    self._live += 1
                    break
                # at the limit with nothing to evict; wait for a release
                self._cond.wait()

        # boot outside the lock: it takes tens of seconds and must not block
        # other workers from acquiring and releasing VMs
        try:
            vm = self._boot(config)
        except BaseException as e:
            with self._cond:
                self._live -= 1
                self._boot_error[config.id] = str(e)
                self._cond.notify_all()
            raise

        with self._cond:
            if self._closed:
                self._live -= 1
                vm.kill()
                raise RuntimeError("VM manager is shut down")
        return vm

    def release(self, config: qemu.KernelConfig, vm: qemu.QemuVM) -> None:
        with self._cond:
            if self._closed:
                vm.kill()
                return
            if vm.alive():
                self._idle.setdefault(config.id, []).append(vm)
            else:
                # QEMU died while the test ran; free the slot for a replacement
                self._live -= 1
            self._cond.notify()

    def _evict_idle(self) -> bool:
        # caller holds _cond; kill one idle VM of any config to free a slot
        for vms in self._idle.values():
            if vms:
                vms.pop().kill()
                self._live -= 1
                return True
        return False

    def shutdown(self) -> None:
        with self._cond:
            self._closed = True
            vms = [vm for group in self._idle.values() for vm in group]
            self._idle.clear()
            self._cond.notify_all()
        # checked-out VMs are killed by their own release(), which sees _closed
        for vm in vms:
            vm.kill()


class QemuSystemTestHost(GDBTestHost):
    def __init__(
        self,
        pwndbg_root: Path,
        pytest_root: Path,
        images_root: Path,
        gdb_path: Path,
    ):
        super().__init__(pwndbg_root, pytest_root, images_root, gdb_path)

        images = kernel_images.discover_images(images_root)
        if not images:
            raise RuntimeError(
                f"no kernel images found in {images_root}; download them with "
                "./tests/library/qemu_system/download-kernel-images.sh"
            )

        # incomplete kernels are skipped with a warning above; fail instead of
        # silently testing fewer kernels
        num_vmlinux = len(list(images_root.glob("vmlinux-*")))
        if len(images) != num_vmlinux:
            raise RuntimeError(
                f"only {len(images)} of {num_vmlinux} kernels in {images_root} are usable; "
                "rerun ./tests/library/qemu_system/download-kernel-images.sh"
            )

        self._configs = {config.id: config for config in qemu.test_configs(images)}
        self._manager = _VmManager(self._boot_vm, _MAX_VMS)

        # make sure no QEMU process outlives the test run
        atexit.register(self.shutdown)

        _warn_if_ptrace_restricted()

    def shutdown(self) -> None:
        self._manager.shutdown()

    def collect(self) -> list[str]:
        cases = super().collect()

        # group cases by config, so a config's VMs are reused across its tests
        # before the next config's VMs boot
        return [f"{case}[{config_id}]" for config_id in self._configs for case in cases]

    def run(
        self,
        case: str,
        coverage_out: Path | None,
        interactive: bool,
    ) -> TestResult:
        # run() must never raise: the parallel runner reads results in a Future
        # callback that swallows exceptions, so a raised test would vanish from
        # the summary and leave the run green
        try:
            base_case, config = self._parse_case(case)
        except Exception as e:
            return TestResult(TestStatus.FAILED, 0, str(e), "", "bad test case")

        try:
            vm = self._manager.acquire(config)
        except Exception as e:
            return TestResult(TestStatus.FAILED, 0, str(e), "", "failed to boot VM")

        try:
            extra_gdb_args = [
                "-ex",
                "set exception-verbose on",
                "-ex",
                f"file {config.image.vmlinux}",
                "-ex",
                f"target remote :{vm.gdb_port}",
            ]
            extra_env = {
                "PWNDBG_ARCH": config.image.arch,
                "PWNDBG_KERNEL_TYPE": config.image.type,
                "PWNDBG_KERNEL_VERSION": config.image.version,
            }
            return self._run_case(base_case, coverage_out, interactive, extra_gdb_args, extra_env)
        except Exception as e:
            return TestResult(TestStatus.FAILED, 0, str(e), "", "kernel test host error")
        finally:
            self._manager.release(config, vm)

    def _parse_case(self, case: str) -> tuple[str, qemu.KernelConfig]:
        """Split a "<pytest case>[<kernel config>]" name produced by
        `collect()` back into its parts.
        """
        base_case, bracket, config_part = case.rpartition("[")
        config_id = config_part.removesuffix("]")
        if not bracket or config_id not in self._configs:
            raise ValueError(f"test case {case!r} does not name a known kernel configuration")
        return base_case, self._configs[config_id]

    def _boot_vm(self, config: qemu.KernelConfig) -> qemu.QemuVM:
        vm = qemu.QemuVM(config)
        try:
            self._advance_guest_to_rest_init(vm)
        except BaseException:
            vm.kill()
            raise
        return vm

    def _advance_guest_to_rest_init(self, vm: qemu.QemuVM) -> None:
        # using 'rest_init' instead of 'start_kernel' to make sure that kernel
        # initialization has progressed sufficiently for testing purposes
        gdb_command = [
            str(self._gdb_path),
            "--silent",
            "--nx",
            "-ex",
            f"file {vm.config.image.vmlinux}",
            "-ex",
            f"target remote :{vm.gdb_port}",
            "-ex",
            "break *rest_init",
            "-ex",
            "continue",
            "-ex",
            "quit",
        ]

        try:
            result = subprocess.run(
                gdb_command,
                cwd=self._pwndbg_root,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=_BOOT_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"guest {vm.config.id} did not reach rest_init within "
                f"{_BOOT_TIMEOUT_S} seconds; console log ({vm.console_log}):\n"
                f"{_console_tail(vm.console_log)}"
            )

        # GDB exits 0 even if 'target remote' or 'continue' failed. Check for a
        # breakpoint *hit* ("Breakpoint 1, ...") rather than the substring
        # "Breakpoint 1", which also matches the "Breakpoint 1 at ..." line
        # printed just for *creating* the breakpoint.
        if "Breakpoint 1, " not in result.stdout:
            raise RuntimeError(
                f"failed to advance guest {vm.config.id} to rest_init; GDB said:\n"
                f"{result.stdout}\n{result.stderr}\n"
                f"console log ({vm.console_log}):\n{_console_tail(vm.console_log)}"
            )
