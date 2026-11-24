#!/usr/bin/env python3
"""Keep GCC's complete atomic ABI, replace only its unsafe word primitives."""
import argparse
import shutil
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument('source', type=Path)
p.add_argument('runtime', type=Path)
a = p.parse_args()
runtime = (a.runtime / 'atomic-runtime.c').read_text()
helpers = runtime[runtime.index('enum lane_operation'):runtime.index('/* Use assembly symbol names')]
helpers = helpers.replace('static uint32_t', 'static inline uint32_t')
helpers = helpers.replace('static volatile uint32_t *', 'static inline volatile uint32_t *')
shutil.copyfile(a.runtime / 'bits/esp32-word-atomic.h', a.source / 'esp32-word-atomic.h')
hook = '''/* S3 PSRAM primitives: all word reads, stores and CAS share the kernel gate.
 * GCC's existing generic and 64-bit pthread fallbacks remain intact.
 */
#include <stdbool.h>
#include <stdint.h>
#include "esp32-word-atomic.h"
''' + helpers + '''
static inline bool esp32_primitive_compare(volatile void *p, void *expected,
                                           uint32_t value, unsigned size)
{
    uint32_t wanted = size == 1 ? *(uint8_t *)expected :
                      size == 2 ? *(uint16_t *)expected : *(uint32_t *)expected;
    uint32_t old = lane_cmpval(p, value, wanted, size);
    if (old == wanted) return true;
    if (size == 1) *(uint8_t *)expected = old;
    else if (size == 2) *(uint16_t *)expected = old;
    else *(uint32_t *)expected = old;
    return false;
}
#undef atomic_compare_exchange_w
#define atomic_compare_exchange_w(p,e,v,w,s,f) esp32_primitive_compare((p),(e),(v),4)
#if defined(N) && N <= 4
#undef HAVE_ATOMIC_TAS_1
#undef HAVE_ATOMIC_TAS_2
#undef HAVE_ATOMIC_TAS_4
#define HAVE_ATOMIC_TAS_1 0
#define HAVE_ATOMIC_TAS_2 0
#define HAVE_ATOMIC_TAS_4 0
#undef atomic_compare_exchange_n
#define atomic_compare_exchange_n(p,e,v,w,s,f) esp32_primitive_compare((p),(e),(v),sizeof(*(p)))
#undef __atomic_compare_exchange_n
#define __atomic_compare_exchange_n(p,e,v,w,s,f) esp32_primitive_compare((p),(e),(v),sizeof(*(p)))
#undef atomic_load_n
#define atomic_load_n(p,m) ((__typeof__(*(p))) lane_load((p),sizeof(*(p))))
#undef __atomic_load_n
#define __atomic_load_n(p,m) atomic_load_n(p,m)
#undef atomic_store_n
#define atomic_store_n(p,v,m) ((void)lane_update((p),(v),sizeof(*(p)),LANE_SET))
#undef __atomic_store_n
#define __atomic_store_n(p,v,m) atomic_store_n(p,v,m)
#undef atomic_exchange_n
#define atomic_exchange_n(p,v,m) ((__typeof__(*(p)))lane_update((p),(v),sizeof(*(p)),LANE_SET))
#undef __atomic_exchange_n
#define __atomic_exchange_n(p,v,m) atomic_exchange_n(p,v,m)
'''
for name, operation, expression in [('add', 'LANE_ADD', 'old + (v)'),
                                   ('sub', 'LANE_SUB', 'old - (v)'),
                                   ('and', 'LANE_AND', 'old & (v)'),
                                   ('or', 'LANE_OR', 'old | (v)'),
                                   ('xor', 'LANE_XOR', 'old ^ (v)'),
                                   ('nand', 'LANE_NAND', '~(old & (v))')]:
    hook += f'''#undef __atomic_fetch_{name}
#define __atomic_fetch_{name}(p,v,m) ((__typeof__(*(p)))lane_update((p),(v),sizeof(*(p)),{operation}))
#undef __atomic_{name}_fetch
#define __atomic_{name}_fetch(p,v,m) ({{ __typeof__(*(p)) old = lane_update((p),(v),sizeof(*(p)),{operation}); (__typeof__(*(p)))({expression}); }})
'''
hook += '#endif\n'
(a.source / 'esp32-kernel-atomics.h').write_text(hook)
header = a.source / 'libatomic_i.h'
text = header.read_text()
needle = '#endif /* LIBATOMIC_H */'
assert text.count(needle) == 1
header.write_text(text.replace(needle, '#include "esp32-kernel-atomics.h"\n\n' + needle))
file = a.source / 'glfree.c'
text = file.read_text()
start = text.index('libat_is_lock_free (')
brace = text.index('{', start)
file.write_text(text[:brace+1] + '\n  return false; /* Explicit software serialization on S3 PSRAM. */\n' + text[brace+1:])
print('PASS GCC ABI retained; 32-bit primitives use the kernel gate and lock-free reporting is corrected')

# GCC can lower atomic_flag operations to these symbols when inline atomics
# are disabled. Export them from the same library as the byte operations.
with file.open('a') as glfree:
    glfree.write("""
bool esp32_flag_test(volatile void *, int) __asm__("__atomic_test_and_set");
bool esp32_flag_test(volatile void *ptr, int ordering)
{
    (void)ordering;
    return lane_update(ptr, __GCC_ATOMIC_TEST_AND_SET_TRUEVAL, 1, LANE_SET) != 0;
}

void esp32_flag_clear(volatile void *, int) __asm__("__atomic_clear");
void esp32_flag_clear(volatile void *ptr, int ordering)
{
    (void)ordering;
    lane_update(ptr, 0, 1, LANE_SET);
}
""")
symbols = a.source / 'libatomic.map'
entries = symbols.read_text()
anchor = '\t__atomic_is_lock_free;\n'
assert entries.count(anchor) == 1
symbols.write_text(entries.replace(anchor, anchor +
    '\t__atomic_test_and_set;\n\t__atomic_clear;\n'))
