/* For testing the malloc_chunk command.
 *
 * Move chunks into each bin type so that the test can run the malloc_chunk command on each different type of free chunk.
 */

#include <stdlib.h>
#include <pthread.h>

#define INTERNAL_SIZE_T size_t
#define SIZE_SZ (sizeof (INTERNAL_SIZE_T))
#define CHUNK_HDR_SZ (2 * SIZE_SZ)
#define mem2chunk(mem) ((void*)(mem) - CHUNK_HDR_SZ)

void break_here(void) {}
void configure_heap_layout(void);
void* thread_func(void*);

void* allocated_chunk = NULL;
void* tcache_chunk = NULL;
void* fast_chunk = NULL;
void* small_chunk = NULL;
void* large_chunk = NULL;
void* unsorted_chunk = NULL;

int main(void)
{
    configure_heap_layout();

    break_here();

    pthread_t thread;
    pthread_create(&thread, NULL, thread_func, NULL);
    pthread_join(thread, NULL);
}

void configure_heap_layout(void)
{
    void* chunks[6] = {0};

    // Request 8 fastbin-sized chunks, free these later to populate both the tcache (if present) and a fastbin.
    for (int i=0; i<6; i++)
    {
        chunks[i] = malloc(0x18);
    }

    void* tcache_ = malloc(0x18);
    void* fast = malloc(0x18);

    // Use request size 0xDF8 (chunk size 0xE00) which is larger than the max tcache size
    // (~0xDC8 on 64-bit). This ensures free() always goes to the unsorted bin, even on
    // glibc 2.42+ which added tcache large bins covering sizes up to ~0xDC8.
    void* remainder_me = malloc(0xDF8);
    malloc(0x18);

    void* large = malloc(0xDF8);
    malloc(0x18);

    void* unsorted = malloc(0xDF8);
    malloc(0x18);

    // Pre-fill tcache for the remainder size (0x3F0) so that when the remainder
    // gets sorted during malloc, it overflows tcache and goes to smallbin.
    // On pre-2.43: TCACHE_FILL_COUNT=7, need 7 chunks. On 2.43: need 16.
    // Allocate 16 to cover both cases (extras are harmless on pre-2.43).
    void* smallfill[16];
    for (int i = 0; i < 16; i++)
        smallfill[i] = malloc(0x3E8);  // request 0x3E8, chunk size 0x3F0
    for (int i = 0; i < 16; i++)
        free(smallfill[i]);  // fills tcache for 0x3F0 size

    // Populate smallbin via remaindering & largebin via sorting.
    // free(remainder_me) -> unsorted bin (0xE00 > max tcache, bypasses all tcache bins).
    // malloc(0xA08) -> takes 0xA10 from the 0xE00 chunk, 0x3F0 remainder stays in unsorted.
    free(remainder_me);
    void* before_remainder = malloc(0xA08);

    // free(large) -> unsorted bin. Now unsorted has: 0x3F0 remainder + 0xE00 large.
    free(large);

    // malloc(0xE08) -> chunk 0xE10, larger than both unsorted entries, forces sorting:
    //   0x3F0 remainder -> tcache full, overflows to smallbin
    //   0xE00 large -> largebin
    //   0xE10 request served from top chunk.
    malloc(0xE08);

    // Populate 0x20 tcachebin (if present) & fastbin.
    // On pre-2.43: 7 fill tcache, 8th overflows to fastbin.
    // On glibc 2.43+: all 8 fit in tcache (TCACHE_FILL_COUNT=16), no fastbins.
    for (int i=0; i<6; i++)
    {
        free(chunks[i]);
    }

    free(tcache_);
    free(fast);

    // Populate the unsortedbin LAST so nothing can sort/consume/consolidate it.
    // 0xE00 > max tcache, so this always goes to unsorted bin.
    free(unsorted);

    allocated_chunk = mem2chunk(before_remainder);
    tcache_chunk = mem2chunk(tcache_);
    fast_chunk = mem2chunk(fast);
    small_chunk = mem2chunk(before_remainder + 0xA10);
    large_chunk = mem2chunk(large);
    unsorted_chunk = mem2chunk(unsorted);
}

void* thread_func(void* args)
{
    // Initialize a 2nd arena by allocating any size chunk.
    malloc(0x18);
    break_here();

    configure_heap_layout();
    break_here();

    pthread_exit(NULL);
}
