from __future__ import annotations

import atexit
import functools
import os
import subprocess
import threading
from pathlib import Path

from ..library.qemu_system import images as kernel_images
from ..library.qemu_system import qemu
from . import TestResult
from . import TestStatus
from .gdb import GDBTestHost

_BOOT_TIMEOUT_S = 180

# every checked-out VM is driven by a GDB session that loads the full vmlinux,
# so the memory cost of a slot is dominated by GDB, not QEMU
_MAX_VMS_PER_CONFIG = min(os.cpu_count() or 1, 4)


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


class _VmPool:
    """A lazily grown pool of booted VMs for one kernel configuration.

    acquire() hands out an idle VM, booting a fresh one if none is idle and
    the pool is below its size limit, and blocking until a release() otherwise.
    """

    def __init__(self, boot: functools.partial[qemu.QemuVM], limit: int):
        self._boot = boot
        self._limit = limit
        self._cond = threading.Condition()
        # protected by _cond:
        self._idle: list[qemu.QemuVM] = []
        self._vms: list[qemu.QemuVM] = []
        self._slots = 0
        self._closed = False

    def acquire(self) -> qemu.QemuVM:
        with self._cond:
            while True:
                if self._closed:
                    raise RuntimeError("VM pool is shut down")
                if self._idle:
                    return self._idle.pop()
                if self._slots < self._limit:
                    self._slots += 1
                    break
                # every VM is checked out and the pool may not grow; wait for
                # a release (or shutdown) to wake us
                self._cond.wait()

        # boot outside the lock: it takes tens of seconds, and the other
        # workers must be able to acquire and release VMs meanwhile
        try:
            vm = self._boot()
        except BaseException:
            with self._cond:
                self._slots -= 1
                self._cond.notify()
            raise

        with self._cond:
            if self._closed:
                # shutdown() ran while we were booting and could not see this
                # VM, so it is ours to kill
                self._slots -= 1
                vm.kill()
                raise RuntimeError("VM pool is shut down")
            self._vms.append(vm)

        return vm

    def release(self, vm: qemu.QemuVM) -> None:
        with self._cond:
            if self._closed:
                return
            if vm.alive():
                self._idle.append(vm)
            else:
                # QEMU died while the test ran; free the slot so the next
                # test boots a replacement
                self._vms.remove(vm)
                self._slots -= 1
            self._cond.notify()

    def shutdown(self) -> None:
        with self._cond:
            self._closed = True
            vms = list(self._vms)
            self._vms.clear()
            self._idle.clear()
            self._cond.notify_all()
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
        self._pools = {
            config_id: _VmPool(functools.partial(self._boot_vm, config), _MAX_VMS_PER_CONFIG)
            for config_id, config in self._configs.items()
        }

        # make sure no QEMU process outlives the test run
        atexit.register(self.shutdown)

        _warn_if_ptrace_restricted()

    def shutdown(self) -> None:
        for pool in self._pools.values():
            pool.shutdown()

    def collect(self) -> list[str]:
        cases = super().collect()

        # group cases by config, so one config's pool drains before the next
        # config starts booting VMs
        return [f"{case}[{config_id}]" for config_id in self._configs for case in cases]

    def run(
        self,
        case: str,
        coverage_out: Path | None,
        interactive: bool,
    ) -> TestResult:
        base_case, config = self._parse_case(case)
        pool = self._pools[config.id]

        try:
            vm = pool.acquire()
        except Exception as e:
            # report a boot failure as this test's failure; raising here would be
            # swallowed by the runner's Future callback and vanish from the summary
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
        finally:
            pool.release(vm)

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

        # GDB exits 0 even if 'target remote' or 'continue' failed, so check
        # for the breakpoint hit instead
        if "Breakpoint 1" not in result.stdout:
            raise RuntimeError(
                f"failed to advance guest {vm.config.id} to rest_init; GDB said:\n"
                f"{result.stdout}\n{result.stderr}\n"
                f"console log ({vm.console_log}):\n{_console_tail(vm.console_log)}"
            )
