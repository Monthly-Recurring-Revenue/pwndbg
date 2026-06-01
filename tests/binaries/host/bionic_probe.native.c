// Minimal probe for the bionic (Android libc) test harness.
//
// Built fully static against the Android NDK by Dockerfile.bionic-test-libs (one
// binary per API level) and by the bionic-spike workflow. A static bionic binary
// embeds its own startup and runs on stock x86_64 Linux, so it launches under
// gdb in CI without an Android device or emulator. The tests break on
// break_here() to confirm it runs and reaches a user breakpoint.

#include <stdlib.h>

void break_here(void) {}

int main(void) {
    // Touch the allocator so the binary exercises bionic's malloc.
    void *p = malloc(64);
    break_here();
    free(p);
    return 0;
}
