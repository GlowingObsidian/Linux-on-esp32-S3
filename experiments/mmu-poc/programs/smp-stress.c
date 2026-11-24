#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <sched.h>
#include <semaphore.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

/* NOMMU uClibc omits this declaration; the linked libfork.so.0 supplies it. */
extern pid_t fork(void);

/* Userspace validation of the port's CPU, synchronization and cache paths.
 * No register writes, scratch erase tests or unbounded persistent writes.
 */
enum { WORDS = 32768, COUNTERS = 1000, HANDOFFS = 1000 };
static pthread_mutex_t mutex = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t condition = PTHREAD_COND_INITIALIZER;
static pthread_cond_t startup_condition = PTHREAD_COND_INITIALIZER;
static pthread_barrier_t barrier;
static sem_t semaphores[2];
static pthread_t threads[2];
static uint32_t *buffers[2];
static volatile unsigned atomic_count, cas_count;
static unsigned mutex_count, turn, handoffs, stopping, rounds, seconds, started, cache_running;
static volatile sig_atomic_t signal_seen, signal_bad;
static __thread volatile unsigned tls_word;
static struct timespec deadline;

static void check(int ok, const char *what)
{
    if (!ok) {
        dprintf(2, "FAIL SMP: %s (errno=%d)\n", what, errno);
        _exit(1);
    }
}

static void signal_handler(int number)
{
    volatile float value = 7.25f;
    (void)number;
    value *= 2.0f;
    if (value != 14.5f) signal_bad = 1;
    signal_seen = 1;
}

static void rendezvous(void)
{
    int ret = pthread_barrier_wait(&barrier);
    check(ret == 0 || ret == PTHREAD_BARRIER_SERIAL_THREAD, "pthread barrier");
}

static void pin(unsigned cpu)
{
    cpu_set_t mask;
    unsigned actual = ~0u;
    CPU_ZERO(&mask); CPU_SET(cpu, &mask);
    check(sched_setaffinity(0, sizeof(mask), &mask) == 0, "CPU affinity");
    check(syscall(SYS_getcpu, &actual, NULL, NULL) == 0 && actual == cpu,
          "execution on requested CPU");
}

static uint32_t pattern(unsigned cpu, unsigned round, unsigned index)
{
    return 0x9e3779b9u ^ (cpu << 25) ^ round * 1664525u ^ index * 16807u;
}

/* Indirect calls exercise the FDPIC function-descriptor and instruction path. */
#define FUNCTION(N) \
    static __attribute__((noinline)) unsigned function##N(unsigned x) \
    { return (x ^ (N * 0x10203u)) + N; }
FUNCTION(1) FUNCTION(2) FUNCTION(3) FUNCTION(4)
FUNCTION(5) FUNCTION(6) FUNCTION(7) FUNCTION(8)
static unsigned (*const functions[])(unsigned) = {
    function1, function2, function3, function4,
    function5, function6, function7, function8
};

static void instruction_check(unsigned seed)
{
    for (unsigned n = 0; n < 8; n++)
        check(functions[n](seed) == (seed ^ ((n + 1) * 0x10203u)) + n + 1,
              "indirect executable function result");
}

static void *worker(void *argument)
{
    unsigned cpu = (unsigned)(uintptr_t)argument, peer = cpu ^ 1;
    pin(cpu);
    tls_word = 0xa55a0000u + cpu;
    check(pthread_mutex_lock(&mutex) == 0, "startup lock");
    while (!started) check(pthread_cond_wait(&startup_condition, &mutex) == 0, "startup wait");
    check(pthread_mutex_unlock(&mutex) == 0, "startup unlock");
    for (unsigned n = 0; n < HANDOFFS; n++) {
        check(pthread_mutex_lock(&mutex) == 0, "handoff mutex lock");
        while (turn != cpu)
            check(pthread_cond_wait(&condition, &mutex) == 0, "condition wait");
        check(handoffs == 2 * n + cpu, "condition handoff ordering");
        handoffs++;
        turn = peer;
        check(pthread_cond_signal(&condition) == 0, "condition signal");
        check(pthread_mutex_unlock(&mutex) == 0, "handoff mutex unlock");
    }
    rendezvous();
    if (cpu == 0) {
        check(clock_gettime(CLOCK_MONOTONIC, &deadline) == 0, "monotonic clock");
        deadline.tv_sec += seconds;
        check(pthread_mutex_lock(&mutex) == 0, "cache start lock");
        cache_running = 1;
        check(pthread_cond_broadcast(&startup_condition) == 0, "cache start broadcast");
        check(pthread_mutex_unlock(&mutex) == 0, "cache start unlock");
    }
    rendezvous();
    for (unsigned round = 0;; round++) {
        struct timespec now;
        if (cpu == 0) {
            check(clock_gettime(CLOCK_MONOTONIC, &now) == 0, "monotonic clock");
            stopping = round && (now.tv_sec > deadline.tv_sec ||
                (now.tv_sec == deadline.tv_sec && now.tv_nsec >= deadline.tv_nsec));
        }
        rendezvous();
        if (stopping) break;
        for (unsigned i = 0; i < WORDS; i++) buffers[cpu][i] = pattern(cpu, round, i);
        rendezvous();
        for (unsigned i = WORDS; i > 0; i--)
            check(buffers[peer][i - 1] == pattern(peer, round, i - 1),
                  "cross-CPU cache/memory visibility");
        rendezvous();
        check(pthread_kill(threads[peer], SIGUSR1) == 0, "thread signal");
        volatile float fp = (float)cpu + 0.5f;
        for (unsigned n = 0; n < COUNTERS; n++) {
            check(pthread_mutex_lock(&mutex) == 0, "counter mutex lock");
            unsigned saved = mutex_count;
            if ((n & 31) == 0) sched_yield();
            mutex_count = saved + 1;
            check(pthread_mutex_unlock(&mutex) == 0, "counter mutex unlock");
            __sync_fetch_and_add(&atomic_count, 1);
            unsigned previous;
            do { previous = __atomic_load_n(&cas_count, __ATOMIC_RELAXED); }
            while (__sync_val_compare_and_swap(&cas_count, previous, previous + 1) != previous);
            fp += 0.25f;
            check(tls_word == 0xa55a0000u + cpu, "thread-local storage");
        }
        check(fp == (float)cpu + 250.5f && !signal_bad, "floating-point context");
        instruction_check(round ^ cpu);
        check(sem_post(&semaphores[peer]) == 0, "semaphore post");
        int ret;
        do { ret = sem_wait(&semaphores[cpu]); } while (ret < 0 && errno == EINTR);
        check(ret == 0, "semaphore wait");
        if ((round & 7) == 0) {
            pin(peer);
            instruction_check(round);
            check(tls_word == 0xa55a0000u + cpu, "TLS across CPU migration");
        }
        pin(cpu);
        rendezvous();
        if (cpu == 0) rounds++;
        struct timespec pause = { .tv_nsec = 1000000 };
        while (nanosleep(&pause, &pause) < 0) check(errno == EINTR, "timer sleep");
    }
    return NULL;
}

static void wait_child(pid_t child, const char *what)
{
    int status;
    pid_t ret;
    do { ret = waitpid(child, &status, 0); } while (ret < 0 && errno == EINTR);
    check(ret == child && WIFEXITED(status) && WEXITSTATUS(status) == 0, what);
}

static void fork_checks(const char *self)
{
    unsigned char *memory = malloc(16384);
    int pipefd[2];
    check(memory != NULL && pipe(pipefd) == 0, "fork test resources");
    memset(memory, 0x5a, 16384);
    fflush(NULL);
    pid_t child = fork();
    check(child >= 0, "fork");
    if (!child) {
        close(pipefd[0]);
        for (unsigned i = 0; i < 16384; i++) check(memory[i] == 0x5a, "child fork snapshot");
        memset(memory, 0xa5, 16384);
        unsigned char token = memory[12345];
        check(write(pipefd[1], &token, 1) == 1, "child pipe write");
        _exit(0);
    }
    close(pipefd[1]);
    unsigned char token;
    ssize_t ret;
    do { ret = read(pipefd[0], &token, 1); } while (ret < 0 && errno == EINTR);
    check(ret == 1 && token == 0xa5, "fork child/parent pipe");
    close(pipefd[0]); wait_child(child, "fork child completion");
    for (unsigned i = 0; i < 16384; i++) check(memory[i] == 0x5a, "fork memory isolation");
    free(memory);
    for (unsigned test = 0; test < 2; test++) {
        child = fork(); check(child >= 0, "fork before exec");
        if (!child) {
            if (!test) execl(self, self, "--exec-child", (char *)NULL);
            else if (access("/bin/busybox", X_OK) == 0)
                execl("/bin/busybox", "busybox", "true", (char *)NULL);
            else execl("/usr/bin/true", "true", (char *)NULL);
            _exit(127);
        }
        wait_child(child, "RAM/self and root executable mapping");
    }
    puts("PASS fork isolation, pipes, waitpid, self/root exec");
}

static void cache_status(void)
{
    const char *path = "/sys/bus/platform/devices/3fcc8000.flash-guard/cache_live";
    char text[1536];
    FILE *file = fopen(path, "r");
    if (!file) { check(errno == ENOENT, "cache status access"); return; }
    check(fgets(text, sizeof(text), file) != NULL, "cache status read");
    fclose(file);
    char *field = strstr(text, " errors=");
    unsigned a, b, c;
    check(field && sscanf(field, " errors=%x,%x,%x", &a, &b, &c) == 3,
          "cache status format");
    /* EXTMEM +0xe4 bits 7..11 are statistics-counter overflow flags.
     * Preserve all fault/unknown bits and both CPU access-error words.
     */
    check(!((a & ~0x00000f80u) | b | c), "hardware cache error latch");
    puts("PASS hardware cache error latches clear");
}

static void storage_check(const char *directory)
{
    char name[512];
    unsigned char data[4096], readback[4096];
    check(snprintf(name, sizeof(name), "%s/.smp-stress-%ld", directory,
                   (long)getpid()) < (int)sizeof(name), "storage path length");
    int fd = open(name, O_CREAT | O_EXCL | O_RDWR, 0600);
    check(fd >= 0, "create exclusive storage test file");
    for (unsigned block = 0; block < 16; block++) {
        for (unsigned i = 0; i < sizeof(data); i++) data[i] = (unsigned char)(i ^ block * 17);
        size_t done = 0;
        while (done < sizeof(data)) {
            ssize_t n = write(fd, data + done, sizeof(data) - done);
            if (n < 0 && errno == EINTR) continue;
            check(n > 0, "storage write"); done += (size_t)n;
        }
    }
    check(fsync(fd) == 0 && lseek(fd, 0, SEEK_SET) == 0, "storage fsync/seek");
    for (unsigned block = 0; block < 16; block++) {
        size_t done = 0;
        while (done < sizeof(readback)) {
            ssize_t n = read(fd, readback + done, sizeof(readback) - done);
            if (n < 0 && errno == EINTR) continue;
            check(n > 0, "storage read"); done += (size_t)n;
        }
        for (unsigned i = 0; i < sizeof(readback); i++)
            check(readback[i] == (unsigned char)(i ^ block * 17), "storage data integrity");
    }
    check(close(fd) == 0 && unlink(name) == 0, "storage cleanup");
    puts("PASS concurrent storage: 64 KiB written once, fsynced, verified and removed");
}

int main(int argc, char **argv)
{
    if (argc == 2 && !strcmp(argv[1], "--exec-child")) {
        instruction_check(0x12345678u); return 0;
    }
    check(argc <= 3, "usage: smp-stress [seconds 1..600] [storage directory]");
    char *end = NULL;
    seconds = argc > 1 ? (unsigned)strtoul(argv[1], &end, 10) : 30;
    check(seconds >= 1 && seconds <= 600 && (!end || !*end), "duration 1..600 seconds");
    alarm(seconds + 45);
    setvbuf(stdout, NULL, _IOLBF, 0);
    cpu_set_t allowed;
    check(sched_getaffinity(0, sizeof(allowed), &allowed) == 0 &&
          CPU_ISSET(0, &allowed) && CPU_ISSET(1, &allowed), "CPU0 and CPU1 available");
    cache_status();
    struct sigaction action = { .sa_handler = signal_handler, .sa_flags = SA_RESTART };
    sigemptyset(&action.sa_mask);
    check(sigaction(SIGUSR1, &action, NULL) == 0, "signal handler installation");
    check(pthread_barrier_init(&barrier, NULL, 2) == 0, "barrier initialization");
    pthread_attr_t attributes;
    check(pthread_attr_init(&attributes) == 0 &&
          pthread_attr_setstacksize(&attributes, 65536) == 0, "64 KiB thread stacks");
    for (unsigned cpu = 0; cpu < 2; cpu++) {
        buffers[cpu] = malloc(WORDS * sizeof(uint32_t));
        check(buffers[cpu] != NULL && sem_init(&semaphores[cpu], 0, 0) == 0,
              "cache buffers and semaphores");
    }
    for (unsigned cpu = 0; cpu < 2; cpu++)
        check(pthread_create(&threads[cpu], &attributes, worker, (void *)(uintptr_t)cpu) == 0,
              "pthread creation");
    check(pthread_mutex_lock(&mutex) == 0, "main startup lock");
    started = 1;
    check(pthread_cond_broadcast(&startup_condition) == 0, "main startup broadcast");
    while (!cache_running) check(pthread_cond_wait(&startup_condition, &mutex) == 0, "cache readiness");
    check(pthread_mutex_unlock(&mutex) == 0, "main startup unlock");
    if (argc == 3) storage_check(argv[2]);
    for (unsigned cpu = 0; cpu < 2; cpu++) check(pthread_join(threads[cpu], NULL) == 0, "pthread join");
    check(rounds > 0 && handoffs == 2 * HANDOFFS, "completed cache and condition phases");
    check(mutex_count == 2 * rounds * COUNTERS && atomic_count == mutex_count && cas_count == mutex_count,
          "shared mutex/atomic totals");
    check(signal_seen && !signal_bad, "signal and floating-point state");
    for (unsigned cpu = 0; cpu < 2; cpu++) { sem_destroy(&semaphores[cpu]); free(buffers[cpu]); }
    pthread_barrier_destroy(&barrier); pthread_attr_destroy(&attributes);
    /* Forkbank confines a banked memory family to one CPU until exec. Run
     * fork checks after the independent-mm SMP and migration workers finish.
     */
    fork_checks(argv[0]);
    cache_status();
    printf("PASS CPU0/CPU1: %u rounds, %u mutex and atomic updates\n", rounds, atomic_count);
    puts("PASS CPU migration, cross-CPU cache visibility, TLS, floating point, signals, timers, barriers, conditions and semaphores");
    return 0;
}
