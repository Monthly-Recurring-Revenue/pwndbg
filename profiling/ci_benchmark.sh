#!/usr/bin/env bash
#
# CI Benchmark runner - measures pwndbg startup time and command performance.
#
# Usage:
#   ./profiling/ci_benchmark.sh [output_dir]                    # general benchmarks
#   ./profiling/ci_benchmark.sh [output_dir] --heap-only        # heap benchmarks only
#   ./profiling/ci_benchmark.sh [output_dir] --glibc-dir /path  # heap with custom glibc
#
# Output:
#   <output_dir>/startup.json    - startup timing data
#   <output_dir>/benchmark.json  - command benchmark data
#   <output_dir>/heap.json       - heap benchmark data (when --heap-only or --glibc-dir)
#   <output_dir>/combined.json   - all results combined with system info

set -eo pipefail

# Source common.sh - try pwndbg root first (for baseline runs from /bench_scripts),
# then fall back to relative path (for normal runs from profiling/)
if [[ -f /pwndbg/scripts/common.sh ]]; then
    source /pwndbg/scripts/common.sh
else
    source "$(dirname "$0")/../scripts/common.sh"
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="/tmp/bench_binaries"
mkdir -p "$BIN_DIR"

# Parse arguments
OUTPUT_DIR="/tmp/benchmark_output"
HEAP_ONLY=false
GLIBC_DIR=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --heap-only)
            HEAP_ONLY=true
            shift
            ;;
        --glibc-dir)
            GLIBC_DIR="$2"
            shift 2
            ;;
        *)
            OUTPUT_DIR="$1"
            shift
            ;;
    esac
done

mkdir -p "$OUTPUT_DIR"

# ── System info ──────────────────────────────────────────────
glibc_version=$(ldd --version 2>&1 | head -1 | grep -oP '[0-9]+\.[0-9]+$' || echo "unknown")
python_version=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')")
gdb_version=$(gdb --version 2>&1 | head -1 || echo "unknown")
kernel_version=$(uname -r)

echo "=== System Info ==="
echo "  glibc:  $glibc_version"
echo "  python: $python_version"
echo "  gdb:    $gdb_version"
echo "  kernel: $kernel_version"
if [[ -n "$GLIBC_DIR" ]]; then
    echo "  custom glibc: $GLIBC_DIR"
fi
echo ""

# Write system info as JSON right away
python3 -c "
import json, sys
json.dump({
    'glibc': sys.argv[1],
    'glibc_custom': sys.argv[2] or None,
    'python': sys.argv[3],
    'gdb': sys.argv[4],
    'kernel': sys.argv[5],
}, open(sys.argv[6], 'w'), indent=2)
" "$glibc_version" "$GLIBC_DIR" "$python_version" "$gdb_version" "$kernel_version" "$OUTPUT_DIR/system.json"

# ── Compile test binaries ────────────────────────────────────
echo "=== Compiling test binaries ==="
gcc -g -o "$BIN_DIR/test" "$SCRIPT_DIR/test.c" 2>/dev/null || \
    cc -g -o "$BIN_DIR/test" "$SCRIPT_DIR/test.c"

# Compile heap test binary
if [[ -n "$GLIBC_DIR" ]]; then
    gcc -g -o "$BIN_DIR/heap_test_bin" "$SCRIPT_DIR/heap_test.c" \
        -Wl,-rpath="$GLIBC_DIR" \
        -Wl,--dynamic-linker="$GLIBC_DIR/ld-linux-x86-64.so.2" \
        2>/dev/null || \
    cc -g -o "$BIN_DIR/heap_test_bin" "$SCRIPT_DIR/heap_test.c" \
        -Wl,-rpath="$GLIBC_DIR" \
        -Wl,--dynamic-linker="$GLIBC_DIR/ld-linux-x86-64.so.2"
    echo "  heap_test_bin linked against glibc in $GLIBC_DIR"
else
    gcc -g -o "$BIN_DIR/heap_test_bin" "$SCRIPT_DIR/heap_test.c" 2>/dev/null || \
        cc -g -o "$BIN_DIR/heap_test_bin" "$SCRIPT_DIR/heap_test.c"
fi
echo "  Done."
echo ""

# ── General benchmarks (skip if --heap-only) ─────────────────
if [[ "$HEAP_ONLY" == "false" ]]; then
    # Measure startup time
    echo "=== Measuring startup time (5 runs) ==="
    startup_times=()
    for i in $(seq 1 5); do
        start_ns=$(date +%s%N)
        $UV_RUN pwndbg "$BIN_DIR/test" --batch -ex 'quit' > /dev/null 2>&1 || true
        end_ns=$(date +%s%N)
        elapsed=$(python3 -c "print(f'{($end_ns - $start_ns) / 1000000000:.6f}')")
        startup_times+=("$elapsed")
        echo "  Run $i: ${elapsed}s"
    done

    # Calculate startup stats
    startup_json=$(python3 -c "
import json, sys
times = [float(t) for t in sys.argv[1:]]
times.sort()
print(json.dumps({
    'min': round(times[0], 6),
    'max': round(times[-1], 6),
    'avg': round(sum(times) / len(times), 6),
    'median': round(times[len(times) // 2], 6),
    'samples': len(times),
}))
" "${startup_times[@]}")
    echo "$startup_json" > "$OUTPUT_DIR/startup.json"
    echo "  Stats: $startup_json"
    echo ""

    # Measure profiled startup (PWNDBG_PROFILE=1)
    echo "=== Measuring profiled startup ==="
    PWNDBG_PROFILE=1 $UV_RUN pwndbg "$BIN_DIR/test" --batch -ex 'quit' > /tmp/profile_output.log 2>&1 || true
    profile_load_time=$(grep -oP 'Time Elapsed: \K[\d.]+' /tmp/profile_output.log | head -1 || echo "0")
    echo "  Profile load time: ${profile_load_time}s"
    echo "$profile_load_time" > "$OUTPUT_DIR/profile_load_time.txt"
    echo ""

    # Run command benchmarks
    echo "=== Running command benchmarks ==="
    export BENCHMARK_OUTPUT="$OUTPUT_DIR/benchmark.json"
    $UV_RUN pwndbg "$BIN_DIR/test" \
        --batch \
        -ex 'b main' -ex 'r' \
        -ex "source $SCRIPT_DIR/ci_benchmark_gdb.py" \
        -ex 'quit' || {
        echo "WARNING: Benchmark script failed, creating empty results"
        echo '{}' > "$OUTPUT_DIR/benchmark.json"
    }
    echo ""
else
    # Create empty placeholders for heap-only mode
    echo '{}' > "$OUTPUT_DIR/startup.json"
    echo '{}' > "$OUTPUT_DIR/benchmark.json"
    echo "0" > "$OUTPUT_DIR/profile_load_time.txt"
fi

# ── Heap benchmarks ──────────────────────────────────────────
echo "=== Running heap benchmarks ==="
export BENCHMARK_OUTPUT="$OUTPUT_DIR/heap.json"
$UV_RUN pwndbg "$BIN_DIR/heap_test_bin" \
    --batch \
    -ex 'b main' -ex 'r' \
    -ex "source $SCRIPT_DIR/ci_heap_benchmark_gdb.py" \
    -ex 'quit' || {
    echo "WARNING: Heap benchmark script failed, creating empty results"
    echo '{}' > "$OUTPUT_DIR/heap.json"
}
echo ""

# ── Combine results ──────────────────────────────────────────
echo "=== Combining results ==="
python3 -c "
import json

system = json.load(open('$OUTPUT_DIR/system.json'))
startup = json.load(open('$OUTPUT_DIR/startup.json'))
benchmark = json.load(open('$OUTPUT_DIR/benchmark.json'))
heap = json.load(open('$OUTPUT_DIR/heap.json'))

plt = open('$OUTPUT_DIR/profile_load_time.txt').read().strip()
profile_load_time = float(plt) if plt and plt != '0' else None

combined = {
    'system': system,
    'startup': startup,
    'profile_load_time': profile_load_time,
    'benchmarks': benchmark,
    'heap_benchmarks': heap,
}

with open('$OUTPUT_DIR/combined.json', 'w') as f:
    json.dump(combined, f, indent=2)
print(json.dumps(combined, indent=2))
"

echo ""
echo "=== Benchmark complete ==="
echo "Results: $OUTPUT_DIR/combined.json"
