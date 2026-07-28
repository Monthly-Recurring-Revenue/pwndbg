from __future__ import annotations

import atexit
import os
import subprocess
from pathlib import Path

from ..library.qemu_system import images as kernel_images
from ..library.qemu_system import qemu
from . import TestResult
from . import TestStatus
from .gdb import GDBTestHost

_BOOT_TIMEOUT_S = 180


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
        # No Yama, nothing to warn about.
        return

    if scope != 0 and os.geteuid() != 0:
        print(
            "WARNING: You are not running as root and ptrace_scope is not set to zero.\n"
            "Some tests will fail to read guest memory through /proc/<qemu-pid>/mem.\n"
            "To fix this, run the following command:\n"
            "\n"
            "    echo 0 | sudo tee /proc/sys/kernel/yama/ptrace_scope\n"
        )


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
        self._vm: qemu.QemuVM | None = None

        # make sure no QEMU process outlives the test run
        atexit.register(self.shutdown)

        _warn_if_ptrace_restricted()

    def shutdown(self) -> None:
        if self._vm is not None:
            self._vm.kill()
            self._vm = None

    def collect(self) -> list[str]:
        cases = super().collect()

        # group cases by config, so one VM boot serves all of a config's tests
        return [f"{case}[{config_id}]" for config_id in self._configs for case in cases]

    def run(
        self,
        case: str,
        coverage_out: Path | None,
        interactive: bool,
    ) -> TestResult:
        base_case, config = self._parse_case(case)

        try:
            vm = self._vm_for(config)
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
            if not vm.alive():
                # QEMU died while the test ran; boot a fresh VM for the next test
                self._vm = None

    def _parse_case(self, case: str) -> tuple[str, qemu.KernelConfig]:
        """Split a "<pytest case>[<kernel config>]" name produced by
        `collect()` back into its parts.
        """
        base_case, bracket, config_part = case.rpartition("[")
        config_id = config_part.removesuffix("]")
        if not bracket or config_id not in self._configs:
            raise ValueError(f"test case {case!r} does not name a known kernel configuration")
        return base_case, self._configs[config_id]

    def _vm_for(self, config: qemu.KernelConfig) -> qemu.QemuVM:
        if self._vm is not None:
            if self._vm.config.id == config.id and self._vm.alive():
                return self._vm
            self._vm.kill()
            self._vm = None

        vm = qemu.QemuVM(config)
        try:
            self._advance_guest_to_rest_init(vm)
        except BaseException:
            vm.kill()
            raise

        self._vm = vm
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
