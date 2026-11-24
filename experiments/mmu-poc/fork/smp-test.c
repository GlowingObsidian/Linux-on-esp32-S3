#define _GNU_SOURCE
#include <errno.h>
#include <sched.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <unistd.h>

extern pid_t fork(void);
static volatile unsigned private_value = 123;

static void check(int ok, const char *what)
{
    if (!ok) {
        dprintf(2, "FAIL SMP fork: %s errno=%d pid=%ld\n", what, errno, (long)getpid());
        _exit(1);
    }
}

static void byte(int fd, int writing)
{
    char token = 'x';
    ssize_t n;
    do { n = writing ? write(fd, &token, 1) : read(fd, &token, 1); }
    while (n < 0 && errno == EINTR);
    check(n == 1, "pipe coordination");
}

static unsigned running_cpu(void)
{
    unsigned cpu;
    check(syscall(SYS_getcpu, &cpu, NULL, NULL) == 0, "getcpu");
    return cpu;
}

int main(int argc, char **argv)
{
    cpu_set_t mask;
    volatile unsigned stack_value = 456;
    int ready[2][2], go[2][2];
    pid_t children[2];
    unsigned i, family_cpu = 0;
    const char *self = argv[0];

    alarm(45);
    check(sysconf(_SC_NPROCESSORS_ONLN) == 2, "two online CPUs required");
    check(sched_getaffinity(0, sizeof(mask), &mask) == 0, "get affinity");
    if (argc == 5 && !strcmp(argv[1], "--exec-worker")) {
        unsigned cpu = (unsigned)atoi(argv[2]);
        int output = atoi(argv[3]), input = atoi(argv[4]);
        volatile unsigned work = 0;

        check(CPU_COUNT(&mask) == 2, "exec must release bank-family affinity");
        CPU_ZERO(&mask);
        CPU_SET(cpu, &mask);
        check(sched_setaffinity(0, sizeof(mask), &mask) == 0, "independent worker placement");
        check(running_cpu() == cpu, "worker on requested CPU");
        byte(output, 1);
        byte(input, 0);
        for (i = 0; i < 2000000; i++) work += i;
        check(running_cpu() == cpu, "worker remains on requested CPU");
        byte(output, 1);
        return 0;
    }

    check(CPU_COUNT(&mask) == 2, "launch without taskset or an inherited CPU restriction");
    for (i = 0; i < 2; i++) {
        check(pipe(ready[i]) == 0 && pipe(go[i]) == 0, "create pipes");
        fflush(NULL);
        children[i] = fork();
        check(children[i] >= 0, "fork");
        check(sched_getaffinity(0, sizeof(mask), &mask) == 0, "get family affinity");
        check(CPU_COUNT(&mask) == 1, "bank family must have exactly one eligible CPU");
        family_cpu = running_cpu();
        check(CPU_ISSET(family_cpu, &mask), "family executes on its eligible CPU");
        if (!children[i]) {
            char cpu[8], output[16], input[16];
            private_value = 999;
            stack_value = 888;
            CPU_ZERO(&mask);
            CPU_SET(1 - family_cpu, &mask);
            errno = 0;
            check(sched_setaffinity(0, sizeof(mask), &mask) == -1 && errno == EINVAL,
                  "affinity cannot move a live bank sibling to the other CPU");
            snprintf(cpu, sizeof(cpu), "%u", i);
            snprintf(output, sizeof(output), "%d", ready[i][1]);
            snprintf(input, sizeof(input), "%d", go[i][0]);
            execl(self, self, "--exec-worker", cpu, output, input, (char *)NULL);
            check(0, "exec independent worker");
        }
        byte(ready[i][0], 0);
        check(private_value == 123 && stack_value == 456, "private global and stack isolation");
    }
    /* Both independently exec'd workers are alive, placed on distinct CPUs,
     * before either receives permission to start the bounded computation.
     */
    for (i = 0; i < 2; i++) byte(go[i][1], 1);
    for (i = 0; i < 2; i++) {
        int status;
        byte(ready[i][0], 0);
        check(waitpid(children[i], &status, 0) == children[i], "wait worker");
        check(WIFEXITED(status) && WEXITSTATUS(status) == 0, "worker exited cleanly");
    }
    puts("PASS SMP fork: isolation, enforced family placement, exec release, workers on both CPUs");
    return 0;
}
