#!/usr/bin/env python3
"""Apply the S3 PSRAM atomic paths to an isolated uClibc build/source copy."""
import argparse
import shutil
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('libc', type=Path)
parser.add_argument('runtime', type=Path)
args = parser.parse_args()
arch = args.libc / 'libc/sysdeps/linux/xtensa'

def replace_file(file, before, after):
    text = file.read_text()
    assert text.count(before) == 1, (file, 'source context changed')
    file.unlink()
    file.write_text(text.replace(before, after, 1))

software = r'''#ifdef __UCLIBC_ESP32_PSRAM_ATOMICS__

/* Native S32C1I cannot serialize PSRAM. Use the explicit S3 kernel gate;
   the compiler/runtime paths use that same gate for atomic loads/stores. */
#include <bits/esp32-word-atomic.h>

#define __arch_compare_and_exchange_val_32_acq(mem, newval, oldval) \
  ((__typeof__(*(mem))) esp32_word_cmpval((volatile uint32_t *)(mem), \
                                        (uint32_t)(newval), (uint32_t)(oldval)))
#define __arch_compare_and_exchange_bool_32_acq(mem, newval, oldval) \
  (!esp32_word_cas((volatile uint32_t *)(mem), (uint32_t)(oldval), (uint32_t)(newval)))
#define __arch_exchange_32_acq(mem, newval) \
  ((__typeof__(*(mem))) esp32_word_call(ESP32_WORD_SET, \
                                      (volatile uint32_t *)(mem), (uint32_t)(newval), 0))
#define __arch_atomic_exchange_and_add_32(mem, value) \
  ((__typeof__(*(mem))) esp32_word_call(ESP32_WORD_ADD, \
                                      (volatile uint32_t *)(mem), (uint32_t)(value), 0))
#define __arch_atomic_exchange_and_sub_32(mem, value) \
  ((__typeof__(*(mem))) esp32_word_call(ESP32_WORD_ADD, \
                                      (volatile uint32_t *)(mem), 0u - (uint32_t)(value), 0))
#define __arch_atomic_decrement_if_positive_32(mem) \
  ({ uint32_t __old; \
     do { __old = esp32_word_load((const volatile uint32_t *)(mem)); \
          if ((int32_t)__old <= 0) break; \
     } while (!esp32_word_cas((volatile uint32_t *)(mem), __old, __old - 1)); \
     (__typeof__(*(mem)))__old; })

#elif XCHAL_HAVE_EXCLUSIVE
'''
replace_file(arch / 'bits/atomic.h', '#if XCHAL_HAVE_EXCLUSIVE\n', software)
replace_file(arch / 'Makefile.arch', 'CSRC-y := brk.c sigaction.c __syscall_error.c\n',
             'CSRC-y := brk.c sigaction.c __syscall_error.c esp32_atomic_runtime.c\n')
shutil.copyfile(args.runtime / 'atomic-runtime.c', arch / 'esp32_atomic_runtime.c')
shutil.copyfile(args.runtime / 'bits/esp32-word-atomic.h', arch / 'bits/esp32-word-atomic.h')

spins = args.libc / 'libpthread/nptl/sysdeps/xtensa'
lock = r'''#ifdef __UCLIBC_ESP32_PSRAM_ATOMICS__
	mov	a3, a2
	movi	a4, 0
	movi	a5, 1
	movi	a6, 4
1:	movi	a2, __NR_xtensa
	syscall
	bltz	a2, 2f
	beqz	a2, 1b
	movi	a2, 0
	abi_ret
2:	neg	a2, a2
	abi_ret
#elif XCHAL_HAVE_EXCLUSIVE
'''
trylock = r'''#ifdef __UCLIBC_ESP32_PSRAM_ATOMICS__
	mov	a3, a2
	movi	a4, 0
	movi	a5, 1
	movi	a6, 4
	movi	a2, __NR_xtensa
	syscall
	bltz	a2, 2f
	beqz	a2, 1f
	movi	a2, 0
	abi_ret
1:	movi	a2, EBUSY
	abi_ret
2:	neg	a2, a2
	abi_ret
#elif XCHAL_HAVE_EXCLUSIVE
'''
for name, branch in [('pthread_spin_lock.S', lock), ('pthread_spin_trylock.S', trylock)]:
    file = spins / name
    replace_file(file, '#if XCHAL_HAVE_EXCLUSIVE\n', branch)
    text = file.read_text()
    file.write_text('#include <asm/unistd.h>\n' + text)
print('PASS explicit libc CAS/add/exchange and both spinlock assembly paths patched')
