// Minimal probe for the bionic (Android libc) testing spike.
//
// Built fully static against the Android NDK's bionic libc.a (see
// .github/workflows/bionic-spike.yml). A static bionic binary embeds its own
// startup and runs on stock x86_64 Linux, so it can be launched under gdb in CI
// without an Android device or emulator. The spike test breaks on break_here()
// to confirm the binary actually runs and reaches a user breakpoint.

#include <stdlib.h>

void break_here(void) {}

int main(void) {
    // Touch the allocator so the binary exercises bionic's malloc.
    void *p = malloc(64);
    break_here();
    free(p);
    return 0;
}
