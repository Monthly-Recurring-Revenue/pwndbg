#include <stdlib.h>
#include <unistd.h>

// Stand-in for coreutils `sleep`, which the attachp tests can't copy under a
// different name on distros shipping a multi-call coreutils (Ubuntu 26.04 uutils).
int main(int argc, char *argv[]) {
    unsigned int seconds = 0;

    for (int i = 1; i < argc; i++) {
        seconds += (unsigned int) atoi(argv[i]);
    }

    sleep(seconds ? seconds : 10);

    return 0;
}
