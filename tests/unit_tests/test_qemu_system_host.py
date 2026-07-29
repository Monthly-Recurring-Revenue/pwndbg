from __future__ import annotations

from pathlib import Path
from typing import Any
from typing import cast

from tests.host import TestHost
from tests.host import TestResult
from tests.host import TestStatus
from tests.host.qemu_system import QemuSystemTestHost
from tests.host.qemu_system import _VmManager
from tests.library.qemu_system import qemu
from tests.library.qemu_system.images import KernelImage


def _config(name: str) -> qemu.KernelConfig:
    image = KernelImage(
        name=name,
        type="linux",
        version="6.6.1",
        arch="x86_64",
        vmlinux=Path(f"vmlinux-{name}"),
        boot_image=Path(f"bzImage-{name}"),
        rootfs=Path("rootfs-x86_64.img"),
    )
    return qemu.KernelConfig(image)


class _FakeVM:
    gdb_port = 1234

    def __init__(self) -> None:
        self.is_alive = True
        self.kill_count = 0

    def alive(self) -> bool:
        return self.is_alive

    def kill(self) -> None:
        self.is_alive = False
        self.kill_count += 1


class _FakeManager:
    def __init__(self) -> None:
        self.vm = _FakeVM()
        self.acquired: list[qemu.KernelConfig] = []
        self.released: list[tuple[qemu.KernelConfig, _FakeVM]] = []

    def acquire(self, config: qemu.KernelConfig) -> qemu.QemuVM:
        self.acquired.append(config)
        return cast(qemu.QemuVM, self.vm)

    def release(self, config: qemu.KernelConfig, vm: qemu.QemuVM) -> None:
        self.released.append((config, cast(_FakeVM, vm)))


class _FailingManager:
    def acquire(self, config: qemu.KernelConfig) -> qemu.QemuVM:
        raise RuntimeError(f"could not boot {config.id}")

    def release(self, config: qemu.KernelConfig, vm: qemu.QemuVM) -> None:
        raise AssertionError("a VM that failed to boot must not be released")


class _FakeQemuHost(QemuSystemTestHost):
    def __init__(self, configs: list[qemu.KernelConfig]) -> None:
        self._configs = {config.id: config for config in configs}
        self._manager = cast(Any, _FakeManager())
        self.case_vms: list[tuple[str, qemu.QemuVM]] = []

    def _run_case_on_vm(
        self,
        base_case: str,
        config: qemu.KernelConfig,
        vm: qemu.QemuVM,
        coverage_out: Path | None,
        interactive: bool,
    ) -> TestResult:
        self.case_vms.append((base_case, vm))
        return TestResult(TestStatus.PASSED, 1, "", "", None)


class _DyingQemuHost(_FakeQemuHost):
    def _run_case_on_vm(
        self,
        base_case: str,
        config: qemu.KernelConfig,
        vm: qemu.QemuVM,
        coverage_out: Path | None,
        interactive: bool,
    ) -> TestResult:
        return QemuSystemTestHost._run_case_on_vm(
            self,
            base_case,
            config,
            vm,
            coverage_out,
            interactive,
        )

    def _run_case(
        self,
        case: str,
        coverage_out: Path | None,
        interactive: bool,
        extra_gdb_args: list[str],
        extra_env: dict[str, str],
    ) -> TestResult:
        manager = cast(_FakeManager, self._manager)
        manager.vm.is_alive = False
        return TestResult(TestStatus.PASSED, 1, "", "", None)


def test_default_test_host_schedules_cases_independently() -> None:
    host = TestHost()

    assert host.execution_groups(["first", "second"]) == [["first"], ["second"]]
    assert host.max_parallelism() is None


def test_qemu_host_groups_cases_by_configuration() -> None:
    first = _config("linux-6.6.1-x86_64")
    second = _config("linux-6.6.2-x86_64")
    host = _FakeQemuHost([first, second])
    cases = [
        f"tests/test_kernel.py::test_one[param][{first.id}]",
        f"tests/test_kernel.py::test_two[{second.id}]",
        f"tests/test_kernel.py::test_three[{first.id}]",
        "malformed-case",
    ]

    assert host.execution_groups(cases) == [
        [cases[0], cases[2]],
        [cases[1]],
        [cases[3]],
    ]
    max_parallelism = host.max_parallelism()
    assert max_parallelism is not None
    assert 1 <= max_parallelism <= 3


def test_qemu_group_uses_one_vm_and_keeps_per_case_results() -> None:
    config = _config("linux-6.6.1-x86_64")
    host = _FakeQemuHost([config])
    cases = [
        f"tests/test_kernel.py::test_one[{config.id}]",
        f"tests/test_kernel.py::test_two[param][{config.id}]",
    ]

    results = host.run_group(cases, None, False)
    manager = cast(_FakeManager, host._manager)

    assert [case for case, _ in results] == cases
    assert [result.status for _, result in results] == [TestStatus.PASSED, TestStatus.PASSED]
    assert [base_case for base_case, _ in host.case_vms] == [
        "tests/test_kernel.py::test_one",
        "tests/test_kernel.py::test_two[param]",
    ]
    assert len({id(vm) for _, vm in host.case_vms}) == 1
    assert manager.acquired == [config]
    assert manager.released == [(config, manager.vm)]


def test_qemu_group_reports_boot_failure_for_every_case() -> None:
    config = _config("linux-6.6.1-x86_64")
    host = _FakeQemuHost([config])
    host._manager = cast(Any, _FailingManager())
    cases = [
        f"tests/test_kernel.py::test_one[{config.id}]",
        f"tests/test_kernel.py::test_two[{config.id}]",
    ]

    results = host.run_group(cases, None, False)

    assert [case for case, _ in results] == cases
    assert all(result.status == TestStatus.FAILED for _, result in results)
    assert all(result.context == "failed to boot VM" for _, result in results)


def test_qemu_group_rejects_mixed_configurations_without_booting() -> None:
    first = _config("linux-6.6.1-x86_64")
    second = _config("linux-6.6.2-x86_64")
    host = _FakeQemuHost([first, second])
    cases = [
        f"tests/test_kernel.py::test_one[{first.id}]",
        f"tests/test_kernel.py::test_two[{second.id}]",
    ]

    results = host.run_group(cases, None, False)
    manager = cast(_FakeManager, host._manager)

    assert [case for case, _ in results] == cases
    assert all(result.status == TestStatus.FAILED for _, result in results)
    assert all(result.context == "bad execution group" for _, result in results)
    assert manager.acquired == []


def test_qemu_exit_turns_a_passing_case_into_a_failure() -> None:
    config = _config("linux-6.6.1-x86_64")
    host = _DyingQemuHost([config])
    case = f"tests/test_kernel.py::test_one[{config.id}]"

    [(result_case, result)] = host.run_group([case], None, False)

    assert result_case == case
    assert result.status == TestStatus.FAILED
    assert result.context == "QEMU exited during test"

    serial_result = host.run(case, None, False)
    assert serial_result.status == TestStatus.FAILED
    assert serial_result.context == "QEMU exited during test"


def test_vm_manager_shutdown_kills_a_checked_out_vm() -> None:
    config = _config("linux-6.6.1-x86_64")
    vm = _FakeVM()
    manager = _VmManager(lambda _: cast(qemu.QemuVM, vm), 1)

    leased_vm = manager.acquire(config)
    manager.shutdown()

    assert leased_vm is vm
    assert not vm.alive()
    assert vm.kill_count == 1
