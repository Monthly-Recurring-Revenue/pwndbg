#!/usr/bin/env python3
"""
Compare two benchmark result files and produce a markdown summary.

Usage:
    python3 compare_benchmarks.py base.json head.json [--threshold 10]

Outputs markdown to stdout suitable for GitHub Actions job summary.
"""

from __future__ import annotations

import argparse
import json
import sys


def format_time(seconds: float) -> str:
    if seconds < 0.001:
        return f"{seconds * 1_000_000:.0f}us"
    if seconds < 1:
        return f"{seconds * 1000:.1f}ms"
    return f"{seconds:.3f}s"


def compare_metric(
    base: dict, head: dict, key: str, label: str
) -> tuple[str, float | None]:
    """Compare a single metric between base and head. Returns (markdown_row, pct_change)."""
    base_val = base.get(key, {}).get("median") if isinstance(base.get(key), dict) else None
    head_val = head.get(key, {}).get("median") if isinstance(head.get(key), dict) else None

    base_str = format_time(base_val) if base_val is not None else "N/A"
    head_str = format_time(head_val) if head_val is not None else "N/A"

    if base_val is None or head_val is None:
        return f"| {label} | {base_str} | {head_str} | - | |", None

    if base_val == 0:
        pct = 0.0
    else:
        pct = ((head_val - base_val) / base_val) * 100

    if pct > 5:
        indicator = "regression"
    elif pct < -5:
        indicator = "improvement"
    else:
        indicator = ""

    row = f"| {label} | {base_str} | {head_str} | {pct:+.1f}% | {indicator} |"
    return row, pct


def _render_comparison_table(
    base: dict, head: dict, metrics: list[tuple[str, str]], threshold: float
) -> tuple[list[str], bool]:
    """Render a comparison table for a set of metrics. Returns (lines, has_regression)."""
    lines = []
    has_regression = False
    lines.append("| Metric | Base (median) | Head (median) | Change | Status |")
    lines.append("|--------|--------------|--------------|--------|--------|")
    for key, label in metrics:
        row, pct = compare_metric(base, head, key, label)
        if pct is not None and pct > threshold:
            has_regression = True
        lines.append(row)
    return lines, has_regression


def _render_single_table(data: dict, metrics: list[tuple[str, str]]) -> list[str]:
    """Render a single-run table for a set of metrics."""
    lines = []
    lines.append("| Metric | Avg | Min | Median | Samples |")
    lines.append("|--------|-----|-----|--------|---------|")
    for key, label in metrics:
        m = data.get(key, {})
        if not isinstance(m, dict):
            continue
        if "error" in m:
            lines.append(f"| {label} | ERROR | - | - | {m['error']} |")
        elif m and "avg" in m:
            lines.append(
                f"| {label} | {format_time(m.get('avg', 0))} "
                f"| {format_time(m.get('min', 0))} "
                f"| {format_time(m.get('median', 0))} "
                f"| {m.get('samples', '?')} |"
            )
    return lines


COMMAND_METRICS = [
    ("context_cold", "Context (cold cache)"),
    ("context_warm", "Context (warm cache)"),
    ("context_step", "Context (with step)"),
    ("regs", "Registers"),
    ("disasm", "Disassembly"),
    ("stack", "Stack"),
    ("backtrace", "Backtrace"),
]

HEAP_METRICS = [
    ("heap", "heap"),
    ("bins", "bins"),
    ("vis_heap_chunks", "vis_heap_chunks"),
    ("arena", "arena"),
    ("mp", "mp"),
    ("top_chunk", "top_chunk"),
    ("heap_config", "heap_config"),
]


def generate_report(
    base_data: dict, head_data: dict, threshold: float
) -> tuple[str, bool]:
    """Generate markdown comparison report. Returns (markdown, has_regression)."""
    lines = []
    has_regression = False

    # System info
    head_sys = head_data.get("system", {})
    glibc_custom = head_sys.get("glibc_custom") or ""
    # Extract version from path like /pwndbg/glibcs/2.35 -> 2.35
    if "/" in glibc_custom:
        glibc_custom = glibc_custom.rstrip("/").rsplit("/", 1)[-1]
    glibc_label = glibc_custom or head_sys.get("glibc", "?")
    lines.append(f"**System**: glibc {glibc_label}, "
                 f"Python {head_sys.get('python', '?')}, "
                 f"{head_sys.get('gdb', '?')}")
    lines.append("")

    # Startup time
    base_startup = base_data.get("startup", {}).get("avg")
    head_startup = head_data.get("startup", {}).get("avg")
    if base_startup and head_startup:
        lines.append("### Startup Time")
        lines.append("")
        lines.append("| Metric | Base | Head | Change | Status |")
        lines.append("|--------|------|------|--------|--------|")
        pct = ((head_startup - base_startup) / base_startup) * 100 if base_startup else 0
        indicator = "regression" if pct > threshold else ("improvement" if pct < -threshold else "")
        if pct > threshold:
            has_regression = True
        lines.append(
            f"| Startup | {format_time(base_startup)} | {format_time(head_startup)} | {pct:+.1f}% | {indicator} |"
        )

        base_profile = base_data.get("profile_load_time")
        head_profile = head_data.get("profile_load_time")
        if base_profile and head_profile:
            pct = ((head_profile - base_profile) / base_profile) * 100 if base_profile else 0
            indicator = "regression" if pct > threshold else ("improvement" if pct < -threshold else "")
            if pct > threshold:
                has_regression = True
            lines.append(
                f"| Profile Load | {format_time(base_profile)} | {format_time(head_profile)} | {pct:+.1f}% | {indicator} |"
            )
        lines.append("")

    # Command benchmarks
    base_bench = base_data.get("benchmarks", {})
    head_bench = head_data.get("benchmarks", {})
    if base_bench or head_bench:
        lines.append("### Command Benchmarks")
        lines.append("")
        table_lines, reg = _render_comparison_table(base_bench, head_bench, COMMAND_METRICS, threshold)
        has_regression = has_regression or reg
        lines.extend(table_lines)
        lines.append("")

    # Heap benchmarks
    base_heap = base_data.get("heap_benchmarks", {})
    head_heap = head_data.get("heap_benchmarks", {})
    if base_heap or head_heap:
        glibc_ver = (head_heap or base_heap).get("glibc_version", "")
        lines.append(f"### Heap Benchmarks (glibc {glibc_ver})")
        lines.append("")
        table_lines, reg = _render_comparison_table(base_heap, head_heap, HEAP_METRICS, threshold)
        has_regression = has_regression or reg
        lines.extend(table_lines)
        lines.append("")

    lines.append(f"*Threshold for regression: >{threshold:.0f}% slower*")

    return "\n".join(lines), has_regression


def generate_single_report(data: dict) -> str:
    """Generate markdown report for a single benchmark run (no comparison)."""
    lines = []

    sys_info = data.get("system", {})
    glibc_custom = sys_info.get("glibc_custom") or ""
    if "/" in glibc_custom:
        glibc_custom = glibc_custom.rstrip("/").rsplit("/", 1)[-1]
    glibc_label = glibc_custom or sys_info.get("glibc", "?")
    lines.append(f"**System**: glibc {glibc_label}, "
                 f"Python {sys_info.get('python', '?')}, "
                 f"{sys_info.get('gdb', '?')}")
    lines.append("")

    # Startup
    startup = data.get("startup", {})
    if startup and startup.get("avg"):
        lines.append("### Startup Time")
        lines.append("")
        lines.append("| Metric | Avg | Min | Median |")
        lines.append("|--------|-----|-----|--------|")
        lines.append(
            f"| Startup | {format_time(startup.get('avg', 0))} "
            f"| {format_time(startup.get('min', 0))} "
            f"| {format_time(startup.get('median', 0))} |"
        )
        profile_load = data.get("profile_load_time")
        if profile_load:
            lines.append(f"| Profile Load | {format_time(profile_load)} | - | - |")
        lines.append("")

    # Command benchmarks
    benchmarks = data.get("benchmarks", {})
    if benchmarks:
        lines.append("### Command Benchmarks")
        lines.append("")
        lines.extend(_render_single_table(benchmarks, COMMAND_METRICS))
        lines.append("")

    # Heap benchmarks
    heap = data.get("heap_benchmarks", {})
    if heap:
        glibc_ver = heap.get("glibc_version", "")
        lines.append(f"### Heap Benchmarks (glibc {glibc_ver})")
        lines.append("")
        lines.extend(_render_single_table(heap, HEAP_METRICS))
        lines.append("")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare benchmark results")
    parser.add_argument("base", help="Base benchmark JSON file (or 'none' for single report)")
    parser.add_argument("head", help="Head benchmark JSON file")
    parser.add_argument(
        "--threshold",
        type=float,
        default=10,
        help="Regression threshold percentage (default: 10)",
    )

    args = parser.parse_args()

    with open(args.head) as f:
        head_data = json.load(f)

    if args.base == "none":
        report = generate_single_report(head_data)
        print(report)
        sys.exit(0)

    with open(args.base) as f:
        base_data = json.load(f)

    report, has_regression = generate_report(base_data, head_data, args.threshold)
    print(report)

    if has_regression:
        print("\n**WARNING: Performance regression detected!**")
        sys.exit(1)


if __name__ == "__main__":
    main()
