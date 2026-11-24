/* SPDX-License-Identifier: MIT */
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "bits/esp32-word-atomic.h"

/* No libc, pthread or compiler atomic calls are used here: this library is
 * also used while the dynamic loader and libc initialize their first locks.
 * All supported scalar loads, stores and RMWs serialize through the kernel.
 * Byte/halfword updates use a masked word CAS and preserve adjacent lanes.
 */
enum lane_operation { LANE_SET, LANE_ADD, LANE_SUB, LANE_AND, LANE_OR,
                      LANE_XOR, LANE_NAND };

static uint32_t lane_mask(unsigned size)
{
    return size == 4 ? UINT32_MAX : (UINT32_C(1) << (size * 8)) - 1;
}

static volatile uint32_t *lane_word(const volatile void *ptr, unsigned size)
{
    uintptr_t address = (uintptr_t)ptr;
    if ((size != 1 && size != 2 && size != 4) || (address & (size - 1)))
        __builtin_trap();
    return (volatile uint32_t *)(address & ~(uintptr_t)3);
}

static uint32_t lane_load(const volatile void *ptr, unsigned size)
{
    unsigned shift = ((uintptr_t)ptr & 3) * 8;
    return (esp32_word_load(lane_word(ptr, size)) >> shift) & lane_mask(size);
}

static uint32_t lane_update(volatile void *ptr, uint32_t argument, unsigned size,
                            enum lane_operation operation)
{
    volatile uint32_t *word = lane_word(ptr, size);
    if (size == 4 && operation != LANE_NAND) {
        unsigned command;
        switch (operation) {
        case LANE_SET: command = ESP32_WORD_SET; break;
        case LANE_ADD: command = ESP32_WORD_ADD; break;
        case LANE_SUB: command = ESP32_WORD_ADD; argument = 0u - argument; break;
        case LANE_AND: command = ESP32_WORD_AND; break;
        case LANE_OR: command = ESP32_WORD_OR; break;
        case LANE_XOR: command = ESP32_WORD_XOR; break;
        default: __builtin_trap();
        }
        return esp32_word_call(command, word, argument, 0);
    }
    unsigned shift = ((uintptr_t)ptr & 3) * 8;
    uint32_t mask = lane_mask(size);
    for (;;) {
        uint32_t old_word = esp32_word_load(word);
        uint32_t old = (old_word >> shift) & mask, next;
        switch (operation) {
        case LANE_SET: next = argument; break;
        case LANE_ADD: next = old + argument; break;
        case LANE_SUB: next = old - argument; break;
        case LANE_AND: next = old & argument; break;
        case LANE_OR: next = old | argument; break;
        case LANE_XOR: next = old ^ argument; break;
        case LANE_NAND: next = ~(old & argument); break;
        default: __builtin_trap();
        }
        uint32_t next_word = (old_word & ~(mask << shift)) | ((next & mask) << shift);
        if (esp32_word_cas(word, old_word, next_word))
            return old;
    }
}

static uint32_t lane_cmpval(volatile void *ptr, uint32_t desired, uint32_t expected,
                            unsigned size)
{
    volatile uint32_t *word = lane_word(ptr, size);
    uint32_t mask = lane_mask(size);
    unsigned shift = ((uintptr_t)ptr & 3) * 8;
    for (;;) {
        uint32_t old_word = esp32_word_load(word);
        uint32_t old = (old_word >> shift) & mask;
        if (old != (expected & mask))
            return old;
        uint32_t next_word = (old_word & ~(mask << shift)) | ((desired & mask) << shift);
        if (esp32_word_cas(word, old_word, next_word))
            return old;
    }
}

/* Use assembly symbol names so definitions cannot be recognized and expanded
 * recursively as GCC builtins. Both C11 __atomic and legacy __sync ABI names
 * are provided for 8-, 16- and 32-bit scalar operations.
 */
#define ALIAS(name, symbol) name __asm__(symbol)
#define SCALAR(N, T) \
T ALIAS(esp_load_##N(const volatile void *, int), "__atomic_load_" #N); \
T esp_load_##N(const volatile void *p, int order) { (void)order; return (T)lane_load(p, N); } \
void ALIAS(esp_store_##N(volatile void *, T, int), "__atomic_store_" #N); \
void esp_store_##N(volatile void *p, T value, int order) { (void)order; lane_update(p, value, N, LANE_SET); } \
T ALIAS(esp_exchange_##N(volatile void *, T, int), "__atomic_exchange_" #N); \
T esp_exchange_##N(volatile void *p, T value, int order) { (void)order; return (T)lane_update(p, value, N, LANE_SET); } \
bool ALIAS(esp_compare_##N(volatile void *, void *, T, int, int), "__atomic_compare_exchange_" #N); \
bool esp_compare_##N(volatile void *p, void *expected, T value, int success, int failure) { \
    (void)success; (void)failure; T wanted = *(T *)expected; \
    T old = (T)lane_cmpval(p, value, wanted, N); \
    if (old == wanted) { return true; } *(T *)expected = old; return false; \
} \
T ALIAS(esp_sync_cmp_##N(volatile void *, T, T), "__sync_val_compare_and_swap_" #N); \
T esp_sync_cmp_##N(volatile void *p, T expected, T value) { return (T)lane_cmpval(p, value, expected, N); } \
bool ALIAS(esp_sync_bool_##N(volatile void *, T, T), "__sync_bool_compare_and_swap_" #N); \
bool esp_sync_bool_##N(volatile void *p, T expected, T value) { return lane_cmpval(p, value, expected, N) == expected; } \
T ALIAS(esp_sync_lock_##N(volatile void *, T), "__sync_lock_test_and_set_" #N); \
T esp_sync_lock_##N(volatile void *p, T value) { return (T)lane_update(p, value, N, LANE_SET); } \
void ALIAS(esp_sync_unlock_##N(volatile void *), "__sync_lock_release_" #N); \
void esp_sync_unlock_##N(volatile void *p) { lane_update(p, 0, N, LANE_SET); }

#define FETCH(N, T, NAME, OP, EXPR) \
T ALIAS(esp_fetch_##NAME##_##N(volatile void *, T, int), "__atomic_fetch_" #NAME "_" #N); \
T esp_fetch_##NAME##_##N(volatile void *p, T value, int order) { (void)order; return (T)lane_update(p, value, N, OP); } \
T ALIAS(esp_##NAME##_fetch_##N(volatile void *, T, int), "__atomic_" #NAME "_fetch_" #N); \
T esp_##NAME##_fetch_##N(volatile void *p, T value, int order) { (void)order; T old = (T)lane_update(p, value, N, OP); return (T)(EXPR); } \
T ALIAS(esp_sync_fetch_##NAME##_##N(volatile void *, T), "__sync_fetch_and_" #NAME "_" #N); \
T esp_sync_fetch_##NAME##_##N(volatile void *p, T value) { return (T)lane_update(p, value, N, OP); } \
T ALIAS(esp_sync_##NAME##_fetch_##N(volatile void *, T), "__sync_" #NAME "_and_fetch_" #N); \
T esp_sync_##NAME##_fetch_##N(volatile void *p, T value) { T old = (T)lane_update(p, value, N, OP); return (T)(EXPR); }

#define WIDTH(N, T) \
SCALAR(N, T) \
FETCH(N, T, add, LANE_ADD, old + value) \
FETCH(N, T, sub, LANE_SUB, old - value) \
FETCH(N, T, and, LANE_AND, old & value) \
FETCH(N, T, or, LANE_OR, old | value) \
FETCH(N, T, xor, LANE_XOR, old ^ value) \
FETCH(N, T, nand, LANE_NAND, ~(old & value))

WIDTH(1, uint8_t)
WIDTH(2, uint16_t)
WIDTH(4, uint32_t)

bool ALIAS(esp_flag_test(volatile void *, int), "__atomic_test_and_set");
bool esp_flag_test(volatile void *ptr, int order)
{
    (void)order;
    return lane_update(ptr, __GCC_ATOMIC_TEST_AND_SET_TRUEVAL, 1, LANE_SET) != 0;
}

void ALIAS(esp_flag_clear(volatile void *, int), "__atomic_clear");
void esp_flag_clear(volatile void *ptr, int order)
{
    (void)order;
    lane_update(ptr, 0, 1, LANE_SET);
}

bool ALIAS(esp_is_lock_free(size_t, const volatile void *), "__atomic_is_lock_free");
bool esp_is_lock_free(size_t size, const volatile void *ptr)
{
    (void)size; (void)ptr;
    return false;
}
