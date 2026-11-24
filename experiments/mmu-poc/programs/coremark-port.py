#!/usr/bin/env python3
"""Adapt only the official CoreMark Linux portability files for 8 MiB RAM."""
from pathlib import Path
import difflib
import sys

source = Path(sys.argv[1])
changes = {
    "linux/core_portme.c": [
        ("#include <stdlib.h>", "#include <stdlib.h>\n#include <errno.h>\n#include <sched.h>\n#include <string.h>"),
        ("clock_gettime(CLOCK_REALTIME,_t)", "clock_gettime(CLOCK_MONOTONIC,_t)"),
        (
            "ee_u8 core_start_parallel(core_results *res) {\n\treturn (ee_u8)pthread_create(&(res->port.thread),NULL,iterate,(void *)res);\n}\nee_u8 core_stop_parallel(core_results *res) {\n\tvoid *retval;\n\treturn (ee_u8)pthread_join(res->port.thread,&retval);\n}",
            """static void thread_check(int rc, const char *operation) {
    if (rc) {
        fprintf(stderr, "CoreMark %s failed: %s\\n", operation, strerror(rc));
        exit(1);
    }
}
static void *coremark_worker(void *arg) {
    core_results *res = (core_results *)arg;
    cpu_set_t mask;
    CPU_ZERO(&mask);
    CPU_SET(res->port.cpu, &mask);
    if (sched_setaffinity(0, sizeof(mask), &mask) < 0)
        thread_check(errno, "CPU affinity");
    return iterate(arg);
}
ee_u8 core_start_parallel(core_results *res) {
    static unsigned next_cpu;
    pthread_attr_t attr;
    res->port.cpu = next_cpu++;
    thread_check(pthread_attr_init(&attr), "pthread_attr_init");
    thread_check(pthread_attr_setstacksize(&attr, 65536), "pthread stack size");
    thread_check(pthread_create(&res->port.thread, &attr, coremark_worker, res), "pthread_create");
    thread_check(pthread_attr_destroy(&attr), "pthread_attr_destroy");
    return 0;
}
ee_u8 core_stop_parallel(core_results *res) {
    void *retval;
    thread_check(pthread_join(res->port.thread, &retval), "pthread_join");
    return 0;
}""",
        ),
    ],
    "linux/core_portme.h": [
        ("pthread_t thread;", "pthread_t thread;\n\tunsigned cpu;"),
    ],
}
diffs = []
for name, replacements in changes.items():
    path = source / name
    before = path.read_text()
    after = before
    for old, new in replacements:
        if after.count(old) != 1:
            raise SystemExit(f"Unexpected CoreMark portability source: {name}")
        after = after.replace(old, new, 1)
    path.write_text(after)
    diffs.extend(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                     fromfile="a/" + name, tofile="b/" + name))
(source.parent / "linux-port.patch").write_text("".join(diffs))
