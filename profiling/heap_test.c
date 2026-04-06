/* Simple heap test binary for benchmarking pwndbg heap commands.
 * Allocates various sizes to create interesting heap state. */

#include <stdlib.h>
#include <string.h>

int main() {
    /* Create a mix of allocations to populate heap structures */
    void *ptrs[64];

    /* Small allocations (tcache/fastbin sized) */
    for (int i = 0; i < 20; i++) {
        ptrs[i] = malloc(16 + i * 8);
        memset(ptrs[i], 'A' + (i % 26), 16 + i * 8);
    }

    /* Medium allocations (smallbin/unsorted) */
    for (int i = 20; i < 40; i++) {
        ptrs[i] = malloc(256 + (i - 20) * 64);
        memset(ptrs[i], 'a' + (i % 26), 256 + (i - 20) * 64);
    }

    /* Large allocations (largebin) */
    for (int i = 40; i < 50; i++) {
        ptrs[i] = malloc(4096 * (i - 39));
    }

    /* Free some to populate bins */
    for (int i = 0; i < 50; i += 2) {
        free(ptrs[i]);
        ptrs[i] = NULL;
    }

    /* Reallocate some to fragment */
    for (int i = 0; i < 50; i += 4) {
        if (ptrs[i] == NULL) {
            ptrs[i] = malloc(32 + i * 4);
        }
    }

    /* breakpoint target */
    void *sentinel = malloc(1);
    (void)sentinel;

    /* Free remaining */
    for (int i = 0; i < 50; i++) {
        free(ptrs[i]);
    }
    free(sentinel);

    return 0;
}
