#define _GNU_SOURCE
#include <errno.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/syscall.h>
#include <unistd.h>

enum { ITERATIONS = 50000 };
static pthread_mutex_t mutex = PTHREAD_MUTEX_INITIALIZER;
static volatile unsigned mutex_count, atomic_count;
static int ready[2], go[2][2];

static void check(int ok, const char *what)
{
    if (!ok) {
        dprintf(2, "FAIL pthread SMP: %s errno=%d\n", what, errno);
        _exit(1);
    }
}

static void token(int fd, int writing)
{
    char c = 'x';
    ssize_t n;
    do { n = writing ? write(fd, &c, 1) : read(fd, &c, 1); }
    while (n < 0 && errno == EINTR);
    check(n == 1, "pipe barrier");
}

static void *worker(void *arg)
{
    unsigned cpu = (unsigned)(unsigned long)arg, actual, i;
    cpu_set_t mask;

    CPU_ZERO(&mask);
    CPU_SET(cpu, &mask);
    check(sched_setaffinity(0, sizeof(mask), &mask) == 0, "thread CPU affinity");
    check(syscall(SYS_getcpu, &actual, NULL, NULL) == 0 && actual == cpu,
          "thread runs on requested CPU");
    token(ready[1], 1);
    token(go[cpu][0], 0);
    for (i = 0; i < ITERATIONS; i++) {
        unsigned value;
        check(pthread_mutex_lock(&mutex) == 0, "mutex lock");
        value = mutex_count;
        /* Make overlapping critical sections visible if locking is broken. */
        for (volatile unsigned delay = 0; delay < 16; delay++) {}
        mutex_count = value + 1;
        check(pthread_mutex_unlock(&mutex) == 0, "mutex unlock");
    }
    token(ready[1], 1);
    token(go[cpu][0], 0);
    for (i = 0; i < ITERATIONS; i++)
        __sync_fetch_and_add(&atomic_count, 1);
    return NULL;
}

int main(void)
{
    pthread_t threads[2];
    pthread_attr_t attr;
    unsigned cpu;
    cpu_set_t allowed;

    alarm(45);
    check(sched_getaffinity(0, sizeof(allowed), &allowed) == 0 &&
          CPU_ISSET(0, &allowed) && CPU_ISSET(1, &allowed),
          "both CPUs must be allowed");
    check(pipe(ready) == 0 && pipe(go[0]) == 0 && pipe(go[1]) == 0,
          "create barriers");
    check(pthread_attr_init(&attr) == 0, "thread attributes");
    check(pthread_attr_setstacksize(&attr, 32768) == 0, "thread stack size");
    for (cpu = 0; cpu < 2; cpu++)
        check(pthread_create(&threads[cpu], &attr, worker,
                             (void *)(unsigned long)cpu) == 0, "create thread");
    token(ready[0], 0); token(ready[0], 0);
    token(go[0][1], 1); token(go[1][1], 1);
    token(ready[0], 0); token(ready[0], 0);
    check(mutex_count == 2 * ITERATIONS, "mutex-protected counter lost updates");
    puts("PASS pthread SMP: shared mutex, workers on CPU0 and CPU1");
    fflush(stdout);
    token(go[0][1], 1); token(go[1][1], 1);
    for (cpu = 0; cpu < 2; cpu++)
        check(pthread_join(threads[cpu], NULL) == 0, "join thread");
    check(atomic_count == 2 * ITERATIONS, "native userspace atomic lost updates");
    puts("PASS pthread SMP: native userspace atomic counter");
    return 0;
}
