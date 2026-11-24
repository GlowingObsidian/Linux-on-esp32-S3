/* SPDX-License-Identifier: MIT */
#ifndef ESP32_WORD_ATOMIC_H
#define ESP32_WORD_ATOMIC_H
#include <stdbool.h>
#include <stdint.h>
#include <asm/unistd.h>

/* The Xtensa legacy atomic syscall is retained for commands 1..4. The S3
 * kernel extends it with a serialized load, bit operations and an ABI query.
 * Native S32C1I on external RAM is not an atomic primitive (IDF cpu.c).
 */
enum esp32_word_command {
    ESP32_WORD_SET = 1, ESP32_WORD_ADD = 2, ESP32_WORD_CAS = 4,
    ESP32_WORD_LOAD = 5, ESP32_WORD_AND = 6, ESP32_WORD_OR = 7,
    ESP32_WORD_XOR = 8, ESP32_WORD_QUERY = 9
};
#define ESP32_WORD_ABI 0x53334131u

#ifndef ESP32_ATOMIC_TEST_CALL
static inline uint32_t esp32_word_raw(unsigned command, volatile uint32_t *ptr,
                                     uint32_t argument, uint32_t replacement)
{
    register uint32_t result __asm__("a2") = __NR_xtensa;
    register uint32_t operation __asm__("a6") = command;
    register uintptr_t address __asm__("a3") = (uintptr_t)ptr;
    register uint32_t arg __asm__("a4") = argument;
    register uint32_t next __asm__("a5") = replacement;
    __asm__ __volatile__("syscall" : "+a"(result)
                         : "a"(operation), "a"(address), "a"(arg), "a"(next)
                         : "memory");
    return result;
}
#else
#define esp32_word_raw ESP32_ATOMIC_TEST_CALL
#endif

static inline uint32_t esp32_word_call(unsigned command, volatile uint32_t *ptr,
                                      uint32_t argument, uint32_t replacement)
{
    /* Fail immediately on an old/mismatched kernel instead of spinning on
     * ENOSYS inside libc's earliest locks. The kernel/runtime are one ABI.
     */
    if (esp32_word_raw(ESP32_WORD_QUERY, 0, 0, 0) != ESP32_WORD_ABI)
        __builtin_trap();
    return esp32_word_raw(command, ptr, argument, replacement);
}

static inline uint32_t esp32_word_load(const volatile uint32_t *ptr)
{
    return esp32_word_call(ESP32_WORD_LOAD, (volatile uint32_t *)ptr, 0, 0);
}

static inline bool esp32_word_cas(volatile uint32_t *ptr, uint32_t expected,
                                uint32_t desired)
{
    uint32_t result = esp32_word_call(ESP32_WORD_CAS, ptr, expected, desired);
    if (result > 1)
        __builtin_trap();
    return result != 0;
}

static inline uint32_t esp32_word_cmpval(volatile uint32_t *ptr,
                                       uint32_t desired, uint32_t expected)
{
    for (;;) {
        uint32_t old = esp32_word_load(ptr);
        if (old != expected || esp32_word_cas(ptr, expected, desired))
            return old;
    }
}
#endif
