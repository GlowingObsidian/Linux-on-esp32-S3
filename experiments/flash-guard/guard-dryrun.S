/* SPDX-License-Identifier: GPL-2.0-only */
/* Relocatable Xtensa windowed-ABI rendezvous. NO flash/cache operations.
 * a2: internal control block, a3: internal stack top,
 * a4: CPU index (0 or 1), a5: timeout in CCOUNT cycles.
 * Fields: ready[2], release, result[2], sp[2], pc[2], elapsed[2].
 * Every access between publication and return is to registers/internal RAM.
 */
    .section .text.esp32_flash_guard_template,"ax",@progbits
    .balign 4
    .global esp32_flash_guard_template_start
    .global esp32_flash_guard_template_end
    .type esp32_flash_guard_template_start,@function
    .begin no-transform
esp32_flash_guard_template_start:
    entry a1, 32
    mov a8, a1
    mov a1, a3
    addi a1, a1, -32
    s32i a2, a1, 0
    s32i a4, a1, 4
    slli a9, a4, 2
    add a14, a2, a9
    s32i a1, a14, 20
    mov a10, a0
    call0 .Lpc
    .balign 4
.Lpc:
    mov a11, a0
    mov a0, a10
    s32i a11, a14, 28
    rsr a12, ccount
    movi a6, 1
    s32i a6, a14, 0
    memw
    bnez a4, .Lpeer
.Lmaster:
    l32i a6, a2, 4
    bnez a6, .Lready
    rsr a13, ccount
    sub a13, a13, a12
    bltu a13, a5, .Lmaster
    j .Labort
.Lready:
    l32i a6, a2, 8
    bnez a6, .Lfinish
    movi a6, 1
    s32i a6, a2, 8
    memw
    j .Lfinish
.Lpeer:
    l32i a6, a2, 8
    bnez a6, .Lfinish
    rsr a13, ccount
    sub a13, a13, a12
    bltu a13, a5, .Lpeer
.Labort:
    movi a6, 2
    s32i a6, a2, 8
    memw
.Lfinish:
    l32i a6, a2, 8
    movi a7, 1
    xor a6, a6, a7
    l32i a7, a1, 4
    beq a7, a4, .Lstack_ok
    movi a6, 4
.Lstack_ok:
    s32i a6, a14, 12
    rsr a13, ccount
    sub a13, a13, a12
    s32i a13, a14, 36
    memw
    mov a1, a8
    mov a2, a6
    retw
esp32_flash_guard_template_end:
    .end no-transform
    .size esp32_flash_guard_template_start, .-esp32_flash_guard_template_start
