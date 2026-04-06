"""
CI Benchmark script - source this inside GDB after hitting a breakpoint.

Usage:
    BENCHMARK_OUTPUT=/tmp/results.json pwndbg ./test \
        --batch -ex 'b main' -ex 'r' -ex 'source ci_benchmark_gdb.py'

Outputs a JSON file with timing data for key pwndbg operations.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import time

import pwndbg.commands.context
import pwndbg.lib.cache


def benchmark(func, iterations=5, clear_cache=True, step=False):
    """Run func multiple times, return timing stats in seconds."""
    times = []
    for _ in range(iterations):
        if clear_cache:
            pwndbg.lib.cache.clear_caches()
        if step:
            gdb.execute("stepi", to_string=True)
        # Suppress pwndbg output during benchmark
        with contextlib.redirect_stdout(io.StringIO()):
            start = time.perf_counter()
            func()
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


def run_benchmarks():
    results = {}

    context_func = pwndbg.commands.context.context.function

    # Context command - cold cache (cache cleared each iteration)
    results["context_cold"] = benchmark(context_func, iterations=5, clear_cache=True)

    # Context command - warm cache (cache reused across iterations)
    results["context_warm"] = benchmark(context_func, iterations=10, clear_cache=False)

    # Context command - with stepping (simulates real debugging)
    results["context_step"] = benchmark(context_func, iterations=5, clear_cache=False, step=True)

    # Individual context components - cold cache
    components = {
        "regs": pwndbg.commands.context.context_regs,
        "disasm": pwndbg.commands.context.context_disasm,
        "stack": pwndbg.commands.context.context_stack,
        "backtrace": pwndbg.commands.context.context_backtrace,
    }
    for name, func in components.items():
        try:
            results[name] = benchmark(func, iterations=5, clear_cache=True)
        except Exception as e:
            results[name] = {"error": str(e)}

    return results


results = run_benchmarks()

output_file = os.environ.get("BENCHMARK_OUTPUT", "/tmp/benchmark_results.json")
with open(output_file, "w") as f:
    json.dump(results, f, indent=2)

# Also print a summary to stdout
print("\n" + "=" * 60)
print("BENCHMARK RESULTS")
print("=" * 60)
for name, data in results.items():
    if "error" in data:
        print(f"  {name:20s}  ERROR: {data['error']}")
    else:
        print(f"  {name:20s}  avg={data['avg']:.4f}s  min={data['min']:.4f}s  median={data['median']:.4f}s")
print("=" * 60)
print(f"Results written to {output_file}")
