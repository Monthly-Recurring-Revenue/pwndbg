#!/usr/bin/env bash

set -o errexit
set -o pipefail

source "$(dirname "$0")/../../../scripts/common.sh"

OUT_DIR=$TESTING_KERNEL_IMAGES_DIR
URL=${URL:-"https://github.com/pwndbg/linux-exploit-dev-env/releases/latest/download"}

mkdir -p "${OUT_DIR}"

wget --no-verbose --show-progress --progress=bar:force:noscroll "${URL}/hashsums.txt" -O "${OUT_DIR}/hashsums.txt"

download() {
    local hash="$1"
    local file="$2"

    # only download file if it doesn't exist or its hashsum has changed
    if echo "${hash}  ${OUT_DIR}/${file}" | sha256sum --check --status 2> /dev/null; then
        return 0
    fi

    # Download to a temporary dotfile first so that an interrupted transfer
    # is never mistaken for a complete image, then rename into place.
    wget --no-verbose --show-progress --progress=bar:force:noscroll "${URL}/${file}" -O "${OUT_DIR}/.${file}.part"
    if ! echo "${hash}  ${OUT_DIR}/.${file}.part" | sha256sum --check --status; then
        echo "Checksum mismatch for ${file}" >&2
        rm -f "${OUT_DIR}/.${file}.part"
        return 1
    fi
    mv "${OUT_DIR}/.${file}.part" "${OUT_DIR}/${file}"
}

pids=()
while read -r hash file; do
    download "${hash}" "${file}" &
    pids+=($!)
done < "${OUT_DIR}/hashsums.txt"

failed=0
for pid in "${pids[@]}"; do
    wait "$pid" || failed=1
done

if [ "$failed" -ne 0 ]; then
    echo "One or more downloads failed."
    exit 1
fi
