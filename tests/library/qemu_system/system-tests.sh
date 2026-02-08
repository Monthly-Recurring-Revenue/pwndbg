#!/usr/bin/env bash

#set -o errexit
set -o pipefail

source "$(dirname "$0")/../../../scripts/common.sh"

ROOT_DIR=$PWNDBG_ABS_PATH
COVERAGERC_PATH="$ROOT_DIR/pyproject.toml"

VMLINUX_LIST=($(basename -a "${TESTING_KERNEL_IMAGES_DIR}"/vmlinux*))

# Ensure we have kimages directory and directories inside
if [ ! -d "$TESTING_KERNEL_IMAGES_DIR" ]; then
    echo "ERROR: The '$TESTING_KERNEL_IMAGES_DIR' directory does not exist. Please run ./download-kernel-images.sh first"
    exit 1
fi
if [ "${VMLINUX_LIST}" = "vmlinux*" ]; then
    echo "ERROR: The '$TESTING_KERNEL_IMAGES_DIR' directory does not contain any kernel images. Please run ./download-kernel-images.sh first"
    exit 1
fi

ptrace_scope=$(cat /proc/sys/kernel/yama/ptrace_scope)
if [[ $ptrace_scope -ne 0 && $(id -u) -ne 0 ]]; then
    cat << EOF
WARNING: You are not running as root and ptrace_scope is not set to zero. If you
run into issues when using pwndbg or gdb-pt-dump, rerun this script as root, or
alternatively run the following command:

    echo 0 | sudo tee /proc/sys/kernel/yama/ptrace_scope

EOF
fi

help_and_exit() {
    echo "Usage: ./system-tests.sh [-p|--pdb] [-c|--cov] [--nix] [--gdb-port=<port>] [-Q|--preserve-qemu-image] [<test-name-filter>]"
    echo "  -p,  --pdb                  enable pdb (Python debugger) post mortem debugger on failed tests"
    echo "  -c,  --cov                  enable codecov"
    echo "  -v,  --verbose              display all test output instead of just failing test output"
    echo "  --nix                       run tests using built for nix environment"
    echo "  --gdb-port=<port>           specify debug port for gdb/QEMU (Default: 1234)"
    echo "  --collect-only              only show the output of test collection, don't run any tests"
    echo "  -Q,  --preserve-qemu-image  don't kill QEMU image after failed tests"
    echo "  <test-name-filter>          run only tests that match the regex"
    exit 1
}

ALL_QEMU_PIDS=()
handle_sigint() {
    echo "Exiting..." >&2
    for pid in "${ALL_QEMU_PIDS[@]}"; do
        echo "Killing QEMU process $pid..." >&2
        pkill -P "$pid" 2>/dev/null
    done
    exit 1
}
trap handle_sigint SIGINT

if [[ $# -gt 3 ]]; then
    help_and_exit
fi

PDB=0
TEST_NAME_FILTER=""
RUN_CODECOV=0
VERBOSE=0
COLLECT_ONLY=0
PRESERVE_QEMU_IMAGE=0
GDB_PORT=1234
RUN_IN_NIX=0

while [[ $# -gt 0 ]]; do
    case $1 in
        -p | --pdb)
            PDB=1
            echo "Will run tests with Python debugger"
            ;;
        -c | --cov)
            echo "Will run codecov"
            RUN_CODECOV=1
            ;;
        -v | --verbose)
            VERBOSE=1
            ;;
        --nix)
            RUN_IN_NIX=1
            ;;
        --collect-only)
            COLLECT_ONLY=1
            ;;
        -Q | --preserve-qemu-image)
            PRESERVE_QEMU_IMAGE=1
            ;;
        --gdb-port=*)
            GDB_PORT="${1#--gdb-port=}"
            ;;
        -h | --help)
            help_and_exit
            ;;
        *)
            if [[ ! -z "${TEST_NAME_FILTER}" ]]; then
                help_and_exit
            fi
            TEST_NAME_FILTER="$1"
            ;;
    esac
    shift
done

NETSTAT=$(which netstat 2> /dev/null)
if [[ -z "${NETSTAT}" ]]; then
    NETSTAT=$(which ss 2> /dev/null)
fi
if [[ -z "${NETSTAT}" ]]; then
    echo "ERROR: netstat/ss not found. Cannot check if ports are already bound." >&2
    exit 1
fi

check_port() {
    local port=$1
    if [[ $(${NETSTAT} -tuln 2> /dev/null | grep ":${port}" | grep -c LISTEN) -ne 0 ]]; then
        echo "ERROR: Port ${port} appears already bound." >&2
        return 1
    fi
    return 0
}

check_port ${GDB_PORT} || exit 1

run_gdb() {
    local arch="$1"
    local should_drop_to_pdb=$2
    shift 2

    if [ $RUN_IN_NIX -eq 1 ]; then
        gdb_load_pwndbg=()

        GDB="$ROOT_DIR/result/bin/pwndbg"
        if [ ! -x "$GDB" ]; then
            echo "ERROR: No nix-compatible pwndbg found. Run nix build .#pwndbg-dev"
            exit 1
        fi
    else
        gdb_load_pwndbg=()

        GDB=pwndbg
    fi

    if [ $should_drop_to_pdb -eq 1 ]; then
        # $GDB --nx "${gdb_load_pwndbg[@]}" \
        #   -ex "set exception-verbose on" "$@"
        echo "Run: "
        echo "$GDB --nx ${gdb_load_pwndbg[@]} -ex \"set exception-debugger on\" -ex \"file ${TESTING_KERNEL_IMAGES_DIR}/vmlinux-${kernel_type}-${kernel_version}-${arch}\" -ex \"target remote :${GDB_PORT}\""
        read -p "Press enter to continue"
    else
        (cd $PWNDBG_ABS_PATH && $UV_RUN_TEST $GDB --silent --nx "${gdb_load_pwndbg[@]}" \
            -ex "set exception-verbose on" "$@" -ex "quit" 2> /dev/null)
    fi
    return $?
}

# NOTE: We run tests under GDB sessions and because of some cleanup/tests dependencies problems
# we decided to run each test in a separate GDB session
gdb_args=("-ex" "py import sys,os; sys.path.insert(0, os.getcwd()); import tests.host.gdb.pytests_collect")
TESTS_COLLECT_OUTPUT=$(TESTS_PATH="$ROOT_DIR/tests/library/qemu_system/tests/" run_gdb "x86_64" 0 "${gdb_args[@]}")

if [ $? -eq 1 ]; then
    echo -E "$TESTS_COLLECT_OUTPUT"
    exit 1
elif [ $COLLECT_ONLY -eq 1 ]; then
    echo "$TESTS_COLLECT_OUTPUT"
    exit 0
fi

TESTS_LIST=($(echo -E "$TESTS_COLLECT_OUTPUT" | grep -o "tests/.*::.*" | grep "${TEST_NAME_FILTER}"))

init_gdb() {
    local kernel_type="$1"
    local kernel_version="$2"
    local arch="$3"

    gdb_connect_qemu=(-ex "file ${TESTING_KERNEL_IMAGES_DIR}/vmlinux-${kernel_type}-${kernel_version}-${arch}" -ex "target remote :${GDB_PORT}")
    # using 'rest_init' instead of 'start_kernel' to make sure that kernel
    # initialization has progressed sufficiently for testing purposes
    gdb_args=("${gdb_connect_qemu[@]}" -ex 'break *rest_init' -ex 'continue')
    run_gdb "${arch}" 0 "${gdb_args[@]}" > /dev/null 2>&1
}

run_test() {
    test_case="$1"
    local kernel_type="$2"
    local kernel_version="$3"
    local arch="$4"
    local should_drop_to_pdb=$5

    gdb_connect_qemu=(-ex "file ${TESTING_KERNEL_IMAGES_DIR}/vmlinux-${kernel_type}-${kernel_version}-${arch}" -ex "target remote :${GDB_PORT}")
    gdb_args=("${gdb_connect_qemu[@]}" "-ex" "py import sys,os; sys.path.insert(0, os.getcwd()); import tests.host.gdb.pytests_launcher")
    if [ ${RUN_CODECOV} -ne 0 ]; then
        gdb_args=(-ex 'py import coverage;coverage.process_startup()' "${gdb_args[@]}")
    fi

    SRC_DIR=$ROOT_DIR \
        COVERAGE_FILE=$ROOT_DIR/.cov/coverage \
        COVERAGE_PROCESS_START=$COVERAGERC_PATH \
        USE_PDB="$should_drop_to_pdb" \
        PWNDBG_LAUNCH_TEST="${test_case}" \
        NO_COLOR=1 \
        PWNDBG_ARCH="${arch}" \
        PWNDBG_KERNEL_TYPE="${kernel_type}" \
        PWNDBG_KERNEL_VERSION="${kernel_version}" \
        run_gdb "${arch}" $should_drop_to_pdb "${gdb_args[@]}"
    return $?
}

process_output() {
    output="$1"
    if [[ -z "$output" ]]; then
        return
    fi

    read -r testname result < <(
        echo "$output" | grep -Po '(^tests/[^ ]+)|(PASSED|FAILED|SKIPPED|XPASS|XFAIL)' \
            | tr '\n' ' ' \
            | cut -d ' ' -f 1,2
    )
    testfile=${testname%::*}
    testname=${testname#*::}

    printf '%-70s %s\n' $testname $result

    # Only show the output of failed tests unless the verbose flag was used
    if [[ $VERBOSE -eq 1 || "$result" =~ FAIL ]]; then
        echo ""
        echo "$output"
        echo ""
    fi

    if [[ "$result" =~ FAIL ]]; then
        FAILED_TESTS+=("$testname")
        return 1
    fi

    return 0
}

test_system() {
    local kernel_type="$1"
    local kernel_version="$2"
    local arch="$3"
    shift 3
    local qemu_args=("$@")

    FAILED_TESTS=()
    printf "============================ Testing %-20s  ============================\n" "${kernel_type}-${kernel_version}-${arch}"

    if [[ ! -z ${qemu_args} ]]; then
        echo "Additional QEMU parameters used: '${qemu_args[@]}'"
    fi
    echo ""

    "./run-qemu-system.sh" --kernel="${kernel_type}-${kernel_version}-${arch}" --gdb-port="${GDB_PORT}" -- "${qemu_args[@]}" > /dev/null &
    QEMU_PID=$!
    ALL_QEMU_PIDS+=($QEMU_PID)
    init_gdb "${kernel_type}" "${kernel_version}" "${arch}"
    start=$(date +%s)

    for t in "${TESTS_LIST[@]}"; do
        output=$(run_test "$t" "${kernel_type}" "${kernel_version}" "${arch}" 0)
        process_output "$output"
        if [ $? -eq 1 ] && [ $PDB -eq 1 ]; then
            run_test "$t" "${kernel_type}" "${kernel_version}" "${arch}" 1
        fi
    done

    end=$(date +%s)
    seconds=$((end - start))
    echo "Tests completed in ${seconds} seconds"

    num_tests_failed=${#FAILED_TESTS[@]}
    num_tests_passed_or_skipped=$((${#TESTS_LIST[@]} - $num_tests_failed))

    echo ""
    echo "*********************************"
    echo "********* TESTS SUMMARY *********"
    echo "*********************************"
    echo "Tests passed or skipped: ${num_tests_passed_or_skipped}"
    echo "Tests failed: ${num_tests_failed}"

    if [ "${num_tests_failed}" -ne 0 ]; then
        echo ""
        echo "Failing tests: ${FAILED_TESTS[@]}"
        echo ""
        if [ ${PRESERVE_QEMU_IMAGE} -eq 0 ]; then
            pkill -P $QEMU_PID 2>/dev/null; kill $QEMU_PID 2>/dev/null
        else
            echo "Preserving qemu image for debugging purposes. Kill with 'pkill -P $QEMU_PID 2>/dev/null; kill $QEMU_PID 2>/dev/null'"
        fi
        exit 1
    fi

    pkill -P $QEMU_PID 2>/dev/null; kill $QEMU_PID 2>/dev/null

}

# Run kernel test configurations in parallel for faster CI.
# Each instance uses a unique GDB port and QEMU runs with snapshot=on
# to allow concurrent access to the same rootfs image.
#
# In PDB mode, fall back to sequential execution since interactive
# debugging requires a single foreground process.
if [ $PDB -eq 1 ]; then
    for vmlinux in "${VMLINUX_LIST[@]}"; do
        KERNEL=$(echo "${vmlinux}" | sed "s/vmlinux-//")
        ARCH="${KERNEL##*-}"
        KERNEL_VERSION=$(echo ${KERNEL} | grep -oP "\d+\.\d+(\.\d+)?(-lts)?")
        KERNEL_TYPE=$(echo ${KERNEL} | sed "s/-${KERNEL_VERSION}-${ARCH}//")

        test_system "${KERNEL_TYPE}" "${KERNEL_VERSION}" "${ARCH}"

        if [[ "${ARCH}" == @("x86_64") ]]; then
            test_system "${KERNEL_TYPE}" "${KERNEL_VERSION}" "${ARCH}" -cpu qemu64,+la57
        fi
    done
else
    BASE_PORT=${GDB_PORT}
    PORT_OFFSET=0
    BG_PIDS=()
    BG_LOGS=()

    for vmlinux in "${VMLINUX_LIST[@]}"; do
        KERNEL=$(echo "${vmlinux}" | sed "s/vmlinux-//")
        ARCH="${KERNEL##*-}"
        KERNEL_VERSION=$(echo ${KERNEL} | grep -oP "\d+\.\d+(\.\d+)?(-lts)?")
        KERNEL_TYPE=$(echo ${KERNEL} | sed "s/-${KERNEL_VERSION}-${ARCH}//")

        CURRENT_PORT=$((BASE_PORT + PORT_OFFSET))
        PORT_OFFSET=$((PORT_OFFSET + 1))
        check_port $CURRENT_PORT || exit 1
        LOG_FILE=$(mktemp)
        BG_LOGS+=("$LOG_FILE")

        (GDB_PORT=$CURRENT_PORT test_system "${KERNEL_TYPE}" "${KERNEL_VERSION}" "${ARCH}") > "$LOG_FILE" 2>&1 &
        BG_PIDS+=($!)

        if [[ "${ARCH}" == @("x86_64") ]]; then
            CURRENT_PORT=$((BASE_PORT + PORT_OFFSET))
            PORT_OFFSET=$((PORT_OFFSET + 1))
            check_port $CURRENT_PORT || exit 1
            LOG_FILE=$(mktemp)
            BG_LOGS+=("$LOG_FILE")

            (GDB_PORT=$CURRENT_PORT test_system "${KERNEL_TYPE}" "${KERNEL_VERSION}" "${ARCH}" -cpu qemu64,+la57) > "$LOG_FILE" 2>&1 &
            BG_PIDS+=($!)
        fi
    done

    echo ""
    echo "Running ${#BG_PIDS[@]} kernel test configurations in parallel..."
    echo "Output will be displayed when all tests complete."
    echo ""

    FINAL_EXIT=0
    for i in "${!BG_PIDS[@]}"; do
        wait ${BG_PIDS[$i]}
        if [ $? -ne 0 ]; then
            FINAL_EXIT=1
        fi
        cat "${BG_LOGS[$i]}"
        rm -f "${BG_LOGS[$i]}"
    done

    exit $FINAL_EXIT
fi
