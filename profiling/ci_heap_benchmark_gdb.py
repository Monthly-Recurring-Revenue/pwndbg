"""
CI Heap Benchmark script - source this inside GDB after hitting a breakpoint
in a binary with heap allocations.

Usage:
    BENCHMARK_OUTPUT=/tmp/heap_results.json pwndbg ./heap_test \
        --batch -ex 'b main' -ex 'r' -ex 'source ci_heap_benchmark_gdb.py'

Benchmarks pwndbg heap commands across different glibc versions.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import time

import gdb

import pwndbg.aglib.heap
import pwndbg.commands.ptmalloc2
import pwndbg.lib.cache


def benchmark(func, iterations=5, clear_cache=True):
    """Run func multiple times, return timing stats in seconds."""
    times = []
    for _ in range(iterations):
        if clear_cache:
            pwndbg.lib.cache.clear_caches()
        with contextlib.redirect_stdout(io.StringIO()):
            start = time.perf_counter()
            try:
                func()
            except Exception:
                pass
            elapsed = time.perf_counter() - start
        times.append(elapsed)

    times.sort()
    return {
        "min": round(times[0], 6),
        "max": round(times[-1], 6),
        "avg": round(sum(times) / len(times), 6),
        "median": round(times[len(times) // 2], 6),
        "samples": len(times),
    }


def run_heap_benchmarks():
    results = {}

    # Run to the sentinel malloc so heap is populated
    gdb.execute("b heap_test.c:46", to_string=True)
    gdb.execute("continue", to_string=True)

    # Detect glibc version
    try:
        glibc_ver = str(pwndbg.aglib.heap.current.libc_version)
    except Exception:
        glibc_ver = "unknown"
    results["glibc_version"] = glibc_ver

    # Benchmark: heap (overview)
    def run_heap():
        gdb.execute("heap", to_string=True)

    results["heap"] = benchmark(run_heap, iterations=5)

    # Benchmark: bins
    def run_bins():
        gdb.execute("bins", to_string=True)

    results["bins"] = benchmark(run_bins, iterations=5)

    # Benchmark: vis_heap_chunks
    def run_vis():
        gdb.execute("vis_heap_chunks", to_string=True)

    results["vis_heap_chunks"] = benchmark(run_vis, iterations=3)

    # Benchmark: heap_config (lightweight)
    def run_heap_config():
        gdb.execute("heap_config", to_string=True)

    results["heap_config"] = benchmark(run_heap_config, iterations=5)

    # Benchmark: arena (if available)
    def run_arena():
        gdb.execute("arena", to_string=True)

    try:
        results["arena"] = benchmark(run_arena, iterations=5)
    except Exception as e:
        results["arena"] = {"error": str(e)}

    # Benchmark: mp (malloc_par)
    def run_mp():
        gdb.execute("mp", to_string=True)

    try:
        results["mp"] = benchmark(run_mp, iterations=5)
    except Exception as e:
        results["mp"] = {"error": str(e)}

    # Benchmark: top_chunk
    def run_top_chunk():
        gdb.execute("top_chunk", to_string=True)

    try:
        results["top_chunk"] = benchmark(run_top_chunk, iterations=5)
    except Exception as e:
        results["top_chunk"] = {"error": str(e)}

    return results


results = run_heap_benchmarks()

output_file = os.environ.get("BENCHMARK_OUTPUT", "/tmp/heap_benchmark_results.json")
with open(output_file, "w") as f:
    json.dump(results, f, indent=2)

# Print summary
print("\n" + "=" * 60)
print(f"HEAP BENCHMARK RESULTS (glibc {results.get('glibc_version', '?')})")
print("=" * 60)
for name, data in results.items():
    if name == "glibc_version":
        continue
    if isinstance(data, dict) and "error" in data:
        print(f"  {name:20s}  ERROR: {data['error']}")
    elif isinstance(data, dict):
        print(f"  {name:20s}  avg={data['avg']:.4f}s  min={data['min']:.4f}s  median={data['median']:.4f}s")
print("=" * 60)
print(f"Results written to {output_file}")
