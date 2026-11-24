#!/usr/bin/env python3
"""Run the real handoff C flow against a lazy IDF mmap/munmap model.

Hardware registers, IDF services and the final Xtensa jump are mocked. The
handoff body and init_linux_mmap are taken from the supplied actual sources.
This proves the ordering regression, not electrical/cache timing on silicon.
"""
from pathlib import Path
import re
import subprocess
import sys
import tempfile


def function(source, name):
    match = re.search(r"^.*\b" + name + r"\([^;{}]*\)\s*\{", source, re.M)
    if not match:
        raise ValueError(name)
    start = source.index("{", match.start())
    depth = 1
    end = start + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[match.start():end]


PREFIX = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <setjmp.h>
#define CONFIG_IDF_TARGET_ESP32S3 1
#define CONFIG_LINUX_PSRAM_8M 1
#define CONFIG_LINUX_COMMAND_LINE 1
#define IRAM_ATTR
#define SOC_SYSTIMER_ALARM_NUM 3
#define ESP_PARTITION_TYPE_ANY 0
#define ESP_PARTITION_SUBTYPE_ANY 0
#define BP_TAG_FIRST 1
#define BP_TAG_LAST 0
#define CMDLINE_MAX 260
struct bp_tag { unsigned id, size; };
#define N_TAGS (3 + CMDLINE_MAX / sizeof(struct bp_tag))
static char space_for_vectors[4096];
static uint32_t mmu[512];
#define DR_REG_MMU_TABLE ((uintptr_t)mmu)
static uint32_t first_free_mmu = ~0U;
static bool missing, jumped;
static bool irqs_masked;
static bool icache_on = true, dcache_on = true, table_cached, psram_dirty = true;
static unsigned cache_writebacks, cache_disables;
static unsigned psram_backing;
static void Cache_WriteBack_All(void) {
    assert(icache_on && dcache_on);
    psram_backing = 0x13579bdf;
    psram_dirty = false;
    cache_writebacks++;
}
static unsigned Cache_Disable_ICache(void) {
    assert(icache_on && !psram_dirty);
    icache_on = false; cache_disables++; return 5;
}
static unsigned Cache_Disable_DCache(void) {
    assert(!icache_on && dcache_on && !psram_dirty);
    dcache_on = false; table_cached = false; cache_disables++; return 9;
}
static void Cache_Enable_DCache(unsigned flag) {
    assert(!icache_on && !dcache_on && flag == 9);
    for (unsigned i = 3; i < 256; ++i) assert(mmu[i] == i);
    dcache_on = true;
}
static void Cache_Enable_ICache(unsigned flag) {
    assert(!icache_on && dcache_on && flag == 5);
    icache_on = true;
}
static unsigned timer_enable[3], timer_irq[3], timer_pending[3];
static int SYSTIMER, TIMERG0, TIMERG1;
enum { WDT_MWDT0, WDT_MWDT1 };
typedef struct { int inst; void *mwdt_dev; } wdt_hal_context_t;
#define wdt_hal_write_protect_disable(x) ((void)(x))
#define wdt_hal_disable(x) ((void)(x))
#define wdt_hal_write_protect_enable(x) ((void)(x))
static void esp_timer_deinit(void) { timer_enable[2] = timer_irq[2] = 0; }
static void systimer_ll_enable_alarm(void *d, unsigned a, bool e) { (void)d; timer_enable[a] = e; }
static void systimer_ll_enable_alarm_int(void *d, unsigned a, bool e) { (void)d; timer_irq[a] = e; }
static void systimer_ll_clear_alarm_int(void *d, unsigned a) { (void)d; timer_pending[a] = 0; }
static void mock_mask_irq(void) { irqs_masked = true; }
static void mock_restore_irq(void) { irqs_masked = false; }
static jmp_buf abort_env;
static void test_abort(void) { longjmp(abort_env, 1); }
#define abort test_abort
static int mock_locked_printf(const char *fmt, ...) {
    (void)fmt;
    if (irqs_masked) test_abort(); /* IDF rejects recursive stdio locks in ISR context. */
    return 0;
}
static int esp_rom_printf(const char *fmt, ...) {
    (void)fmt;
    assert(icache_on && dcache_on);
    return 0; /* ROM output does not use FreeRTOS/newlib locks. */
}
#define printf mock_locked_printf
typedef struct { unsigned address; } esp_partition_t;
typedef void *esp_partition_iterator_t;
static const esp_partition_t linux_partition = { .address = 0x140000 };
static esp_partition_iterator_t esp_partition_find(int type, int subtype, const char *name) {
    (void)type; (void)subtype; assert(!strcmp(name, "linux"));
    if (missing) return NULL;
    /* IDF's existing allocator does not know init_linux_mmap changed its
     * free page: load_partitions maps a table page and unmaps it afterward. */
    mmu[0x83] = 0;
    table_cached = true;
    mmu[0x83] = 0x4000;
    return (void *)&linux_partition;
}
static const esp_partition_t *esp_partition_get(esp_partition_iterator_t it) { return it; }
static void mock_jump(const void *linux, struct bp_tag *tags) {
    assert(icache_on && dcache_on);
    assert((uintptr_t)linux == 0x42140000);
    assert(tags[0].id == BP_TAG_FIRST && tags[N_TAGS - 1].id == BP_TAG_LAST);
    jumped = true;
}
'''

MAIN = r'''
int main(void) {
    for (unsigned i = 0; i < 512; ++i) mmu[i] = 0x4000;
    mmu[0] = 2; mmu[1] = 3; mmu[2] = 1;
    /* IDF reserves its three firmware pages, then the Nano's eight MiB of
     * PSRAM. Its first dynamic flash slot is therefore entry 0x83. */
    for (unsigned i = 3; i < 0x83; ++i) mmu[i] = 0x8000 + i - 3;
    for (unsigned a = 0; a < 3; ++a) timer_enable[a] = timer_irq[a] = timer_pending[a] = 1;
    if (setjmp(abort_env)) {
        assert(EXPECT_BOOT_ABORT && !jumped && irqs_masked && icache_on && dcache_on);
        return 0;
    }
    map_flash_and_go();
    assert(!EXPECT_BOOT_ABORT);
    assert(jumped);
    for (unsigned i = 3; i < 256; ++i)
        assert(mmu[i] == ((EXPECT_BAD && i == 0x83) ? 0x4000 : i));
    for (unsigned i = 0x100; i < 0x180; ++i) assert(mmu[i] == 0x4000);
    for (unsigned i = 0x180; i < 0x200; ++i) assert(mmu[i] == 0x8000 + (i & 0x7f));
    assert(table_cached == !EXPECT_CACHE_FIX);
    if (EXPECT_CACHE_FIX) {
        assert(!psram_dirty && psram_backing == 0x13579bdf);
        assert(cache_writebacks == 1 && cache_disables == 2);
    }
    if (!EXPECT_BAD) {
        for (unsigned a = 0; a < 3; ++a)
            assert(!timer_enable[a] && !timer_irq[a] && !timer_pending[a]);
        missing = true; jumped = false;
        mmu[0x83] = 0x4000;
        if (!setjmp(abort_env)) { map_flash_and_go(); return 3; }
        assert(!jumped && mmu[0x83] == 0x4000);
        if (EXPECT_CACHE_FIX) {
            missing = false;
            unsigned before_writebacks = cache_writebacks;
            mmu[0x20] = 0x20; /* A flash page after the PSRAM/free prefix. */
            if (!setjmp(abort_env)) { map_flash_and_go(); return 4; }
            assert(!jumped && mmu[3] == 3 && mmu[0x83] == 0x4000);
            assert(cache_writebacks == before_writebacks && icache_on && dcache_on);
        }
    }
    return 0;
}
'''

before, after, ll_before, ll_after, bad_console, ll_bad_console = (Path(arg).read_text() for arg in sys.argv[1:])
with tempfile.TemporaryDirectory(prefix="handoff-model-") as tmp:
    for label, source, ll, expected, cache_fix, boot_abort in (
            ("before", before, ll_before, 1, 0, 0),
            ("bad-console", bad_console, ll_bad_console, 0, 1, 1),
            ("after", after, ll_after, 0, 1, 0)):
        body = function(source, "map_flash_and_go")
        masking = 'mock_restore_irq();' if 'wsr a2, ps' in body else 'mock_mask_irq();'
        body = re.sub(r'__asm__\s+__volatile__\s*\(.*?\);', masking, body, flags=re.S)
        body = re.sub(r'asm\s+volatile\s*\(.*?\);', 'mock_jump(linux, tag); return;', body, flags=re.S)
        model = PREFIX + function(source, "find_partition") + "\n"
        mmap_body = function(ll, "init_linux_mmap")
        mmap_body = re.sub(r'__asm__\s+__volatile__\s*\(.*?\);', '(void)0;', mmap_body, flags=re.S)
        model += mmap_body + "\n" + body + "\n" + MAIN
        cfile = Path(tmp) / f"{label}.c"
        exe = Path(tmp) / label
        cfile.write_text(model)
        subprocess.run(["cc", "-std=gnu17", "-O2", f"-DEXPECT_BAD={expected}",
                        f"-DEXPECT_CACHE_FIX={cache_fix}",
                        f"-DEXPECT_BOOT_ABORT={boot_abort}",
                        str(cfile), "-o", str(exe)], check=True)
        subprocess.run([str(exe)], check=True, stdout=subprocess.DEVNULL)
print("PASS: reproduced original invalid entry 0x83 and the exact masked-IRQ stdio abort; ROM output reaches Linux with every mapping/cache mode preserved, dirty-data writeback, stopped alarms and fail-closed preflight")
