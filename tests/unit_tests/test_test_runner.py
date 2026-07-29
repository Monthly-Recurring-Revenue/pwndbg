from __future__ import annotations

import concurrent.futures
from pathlib import Path

import pytest

from tests import tests as runner
from tests.host import TestHost
from tests.host import TestResult
from tests.host import TestStatus


class _RunnerHost(TestHost):
    def __init__(
        self,
        cases: list[str],
        *,
        parallelism: int | None = None,
        raise_from_group: bool = False,
    ) -> None:
        self._cases = cases
        self._parallelism = parallelism
        self._raise_from_group = raise_from_group
        self.grouped_cases: list[str] | None = None
        self.run_calls: list[tuple[str, Path | None, bool]] = []

    def collect(self) -> list[str]:
        return self._cases

    def execution_groups(self, cases: list[str]) -> list[list[str]]:
        self.grouped_cases = list(cases)
        groups: dict[str, list[str]] = {}
        for case in cases:
            groups.setdefault(case.partition(":")[0], []).append(case)
        return list(groups.values())

    def max_parallelism(self) -> int | None:
        return self._parallelism

    def run(self, case: str, coverage_out: Path | None, interactive: bool) -> TestResult:
        self.run_calls.append((case, coverage_out, interactive))
        return TestResult(TestStatus.PASSED, 1, "", "", None)

    def run_group(
        self,
        cases: list[str],
        coverage_out: Path | None,
        interactive: bool,
    ) -> list[tuple[str, TestResult]]:
        if self._raise_from_group:
            raise RuntimeError("group failed")
        return super().run_group(cases, coverage_out, interactive)


def _ignore_signal_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner.signal, "signal", lambda *_: None)


def test_parallel_runner_filters_before_grouping(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _ignore_signal_reset(monkeypatch)
    coverage_out = Path("coverage")
    host = _RunnerHost(["a:keep-one", "a:drop", "b:keep-two"])

    runner.run_tests_and_print_stats(
        host,
        "keep",
        False,
        False,
        2,
        False,
        coverage_out,
    )

    assert host.grouped_cases == ["a:keep-one", "b:keep-two"]
    assert sorted(host.run_calls) == [
        ("a:keep-one", coverage_out, False),
        ("b:keep-two", coverage_out, False),
    ]
    assert "Tests Passed : 2" in capsys.readouterr().out


def test_parallel_runner_caps_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ignore_signal_reset(monkeypatch)
    monkeypatch.setattr(runner, "_available_cpu_count", lambda: 8)
    real_executor = concurrent.futures.ThreadPoolExecutor
    worker_counts: list[int] = []

    def recording_executor(*, max_workers: int) -> concurrent.futures.ThreadPoolExecutor:
        worker_counts.append(max_workers)
        return real_executor(max_workers=max_workers)

    monkeypatch.setattr(runner.concurrent.futures, "ThreadPoolExecutor", recording_executor)
    host = _RunnerHost(["a:one", "b:two", "c:three", "d:four"], parallelism=2)

    runner.run_tests_and_print_stats(host, None, False, False, 3, False, None)

    assert worker_counts == [2]


def test_parallel_runner_reports_every_case_when_a_group_raises(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _ignore_signal_reset(monkeypatch)
    host = _RunnerHost(["a:one", "a:two"], raise_from_group=True)

    with pytest.raises(SystemExit) as exit_info:
        runner.run_tests_and_print_stats(host, None, False, False, 2, False, None)

    assert exit_info.value.code == 1
    output = capsys.readouterr().out
    assert "Tests Failed : 2" in output
    assert "- a:one" in output
    assert "- a:two" in output


def test_serial_runner_preserves_order_and_forwards_options() -> None:
    coverage_out = Path("coverage")
    host = _RunnerHost(["a:first", "b:second"])

    runner.run_tests_and_print_stats(
        host,
        None,
        True,
        True,
        3,
        False,
        coverage_out,
    )

    assert host.grouped_cases is None
    assert host.run_calls == [
        ("a:first", coverage_out, True),
        ("b:second", coverage_out, True),
    ]
