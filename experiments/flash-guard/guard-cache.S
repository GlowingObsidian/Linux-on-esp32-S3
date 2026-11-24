/* SPDX-License-Identifier: GPL-2.0-only */
/* Manual non-writing cache gate. Derived from pinned S3 ROM suspend/resume
 * bodies and IDF's ESP_ROM_HAS_CACHE_SUSPEND_WAITI_BUG workaround.
 * C masks INTENABLE, spills live windows while caches are on, then calls us.
 * No windowed calls or external-memory accesses after entry. The opt-in
 * native backend may invoke a separately verified internal call0 routine.
 * CPU0 alone publishes final. A timed-out peer stays internal until final.
 * An irrecoverable freeze/restore failure stays internal until user reset.
 */
#include "esp32s3-flash-guard-layout.h"

#ifdef CONFIG_MTD_ESP32S3_NOR
#define FG_FRAME 64
#else
#define FG_FRAME 32
#endif

    .section .text.esp32_flash_cache_template,"ax",@progbits
    .balign 4
    .global esp32_flash_cache_template_start
    .global esp32_flash_cache_template_end
    .type esp32_flash_cache_template_start,@function
    .begin no-transform

    .macro FG_READ reg, off
    memw
    l32i \reg, a3, \off
    .endm
    .macro FG_WRITE reg, off
    memw
    s32i \reg, a3, \off
    memw
    .endm
    .macro FG_BNEZ reg, target
    beqz \reg, .Lzero_\@
    j \target
.Lzero_\@:
    .endm
    .macro FG_FAULT_STATUS reg, scratch
    /* Mask only the five documented counter-overflow bits. Raw snapshots
     * retain them; real/unknown faults and per-CPU status stay fail-closed. */
    extui \scratch, \reg, 7, 5
    slli \scratch, \scratch, 7
    xor \reg, \reg, \scratch
    .endm
    .macro FG_POLL off, shift, bits, value, fail
    rsr a10, ccount
.Lpoll_\@:
    FG_READ a7, \off
    extui a7, a7, \shift, \bits
    movi a8, \value
    beq a7, a8, .Lpolled_\@
    rsr a11, ccount
    sub a11, a11, a10
    movi a8, 240
    slli a8, a8, 10
    bltu a11, a8, .Lpoll_\@
    j \fail
.Lpolled_\@:
    .endm
    .macro FG_SNAPSHOT dest, ctrl, bus, auto, freeze
    FG_READ a7, \ctrl
    s32i a7, a2, \dest
    FG_READ a7, \bus
    s32i a7, a2, (\dest + 4)
    FG_READ a7, \auto
    s32i a7, a2, (\dest + 8)
    FG_READ a7, \freeze
    s32i a7, a2, (\dest + 12)
    .endm
    .macro FG_PREFLIGHT saved, auto, preload, freeze
    l32i a7, a2, \saved
    bbsi a7, 0, .Lenabled_\@
    j .Lprecondition
.Lenabled_\@:
    l32i a7, a2, (\saved + 4)
    extui a7, a7, 0, 2
    FG_BNEZ a7, .Lprecondition
    l32i a7, a2, (\saved + 8)
    bbci a7, 9, .Lbuffer_clear_\@
    j .Lprecondition
.Lbuffer_clear_\@:
    l32i a7, a2, (\saved + 12)
    extui a7, a7, 0, 1
    FG_BNEZ a7, .Lprecondition
    FG_POLL \freeze, 2, 1, 0, .Lpreflight_timeout
    FG_POLL \auto, 3, 1, 1, .Lpreflight_timeout
    FG_POLL \preload, 1, 1, 1, .Lpreflight_timeout
    .endm
    .macro FG_SUSPEND saved, ctrl, bus, auto, preload, freeze, shift
    l32i a7, a2, (\saved + 4)
    movi a8, 3
    or a7, a7, a8
    FG_WRITE a7, \bus
    l32i a7, a2, \saved
    movi a8, -2
    and a7, a7, a8
    FG_WRITE a7, \ctrl
    l32i a7, a2, (\saved + 8)
    bbci a7, 2, .Lautoload_off_\@
    movi a8, -5
    and a7, a7, a8
    FG_WRITE a7, \auto
    movi a8, 512
    or a7, a7, a8
    FG_WRITE a7, \auto
    FG_POLL \auto, 3, 1, 1, .Lrestore_timeout
    FG_READ a7, \auto
    movi a8, -513
    and a7, a7, a8
    FG_WRITE a7, \auto
.Lautoload_off_\@:
    FG_POLL \auto, 3, 1, 1, .Lrestore_timeout
    FG_POLL \preload, 1, 1, 1, .Lrestore_timeout
    l32i a7, a2, (\saved + 12)
    movi a8, -3
    and a7, a7, a8
    FG_WRITE a7, \freeze
    movi a8, 1
    or a7, a7, a8
    FG_WRITE a7, \freeze
    FG_POLL \freeze, 2, 1, 1, .Lrestore_timeout
    FG_READ a7, \freeze
    movi a8, -2
    and a7, a7, a8
    FG_WRITE a7, \freeze
    FG_POLL \freeze, 2, 1, 0, .Lrestore_timeout
    FG_POLL 0x130, \shift, 12, 1, .Lrestore_timeout
    .endm
    .macro FG_RESTORE saved, ctrl, bus, auto, preload, freeze
    FG_READ a7, \freeze
    movi a8, -2
    and a7, a7, a8
    FG_WRITE a7, \freeze
    FG_POLL \freeze, 2, 1, 0, .Lfatal
    l32i a7, a2, (\saved + 12)
    FG_WRITE a7, \freeze
    FG_READ a7, \auto
    /* Keep BUFFER_CLEAR asserted until the outstanding queue is done.
     * An IDLE cache FSM alone does not prove preload/autoload completion.
     */
    movi a8, -5
    and a7, a7, a8
    FG_WRITE a7, \auto
    FG_POLL \auto, 3, 1, 1, .Lfatal
    FG_POLL \preload, 1, 1, 1, .Lfatal
    FG_READ a7, \auto
    movi a8, -513
    and a7, a7, a8
    FG_WRITE a7, \auto
    l32i a7, a2, \saved
    FG_WRITE a7, \ctrl
    l32i a7, a2, (\saved + 4)
    FG_WRITE a7, \bus
    l32i a7, a2, (\saved + 8)
    FG_WRITE a7, \auto
    .endm
    .macro FG_COMPARE saved, after, mask
    l32i a7, a2, \saved
    l32i a8, a2, \after
    xor a7, a7, a8
    movi a8, \mask
    and a7, a7, a8
    bnez a7, .Lfatal
    .endm
    .macro FG_ERRORS dest
    FG_READ a7, 0xe4
    s32i a7, a2, \dest
    FG_READ a7, 0xf0
    s32i a7, a2, (\dest + 4)
    FG_READ a7, 0xfc
    s32i a7, a2, (\dest + 8)
    .endm

esp32_flash_cache_template_start:
    entry a1, FG_FRAME
    mov a8, a1
    mov a1, a3
    addi a1, a1, -FG_FRAME
    s32i a8, a1, 0
    s32i a0, a1, 4
    s32i a5, a1, 8
    movi a6, 0
    s32i a6, a1, 12
    slli a9, a4, 2
    add a14, a2, a9
    s32i a1, a14, FGC_SP
    call0 .Linternal_pc
    .balign 4
.Linternal_pc:
    mov a7, a0
    s32i a7, a14, FGC_PC
    l32i a0, a1, 4
    rsr a12, ccount
    rsr a7, intenable
    s32i a7, a14, FGC_IRQ_OFF
    bnez a7, .Lunsafe_entry
    rsr a7, interrupt
    extui a7, a7, 14, 1
    s32i a7, a14, FGC_NMI
    bnez a7, .Lunsafe_entry
    movi a6, 1
    j .Lpublish_ready
.Lunsafe_entry:
    movi a6, 2
.Lpublish_ready:
    s32i a6, a14, FGC_READY
    memw
    FG_BNEZ a4, .Lpeer
    /* Construct MMIO base without literals or relocations. */
    movi a3, 0x600
    slli a3, a3, 20
    movi a8, 0xc4
    slli a8, a8, 12
    add a3, a3, a8
.Lmaster_wait:
    l32i a7, a2, FGC_READY
    movi a8, 1
    beq a7, a8, .Lmaster_usable
    j .Lprecondition
.Lmaster_usable:
    l32i a7, a2, (FGC_READY + 4)
    beq a7, a8, .Lmaster_ready
    FG_BNEZ a7, .Lprecondition
    l32i a7, a2, FGC_ABORT
    FG_BNEZ a7, .Lpreflight_timeout
    rsr a11, ccount
    sub a11, a11, a12
    bltu a11, a5, .Lmaster_wait
    j .Lpreflight_timeout
.Lmaster_ready:
    l32i a7, a2, FGC_ABORT
    FG_BNEZ a7, .Lpreflight_timeout
    /* No driver may leave a live peripheral source routed to true NMI14. */
    movi a8, 32
    slli a8, a8, 8
    sub a9, a3, a8
    movi a6, 2
.Lmatrix:
    movi a10, FG_IRQ_SOURCES
.Lroute:
    memw
    l32i a7, a9, 0
    extui a7, a7, 0, 5
    movi a8, 14
    bne a7, a8, .Lroute_usable
    j .Lprecondition
.Lroute_usable:
    addi a9, a9, 4
    addi a10, a10, -1
    bnez a10, .Lroute
    movi a8, FG_IRQ_SOURCES
    slli a8, a8, 2
    sub a9, a9, a8
    movi a8, 8
    slli a8, a8, 8
    add a9, a9, a8
    addi a6, a6, -1
    bnez a6, .Lmatrix
    movi a6, 1
    s32i a6, a2, FGC_STAGE
    FG_SNAPSHOT FGC_BEFORE, 0x60, 0x64, 0xa0, 0x154
    FG_SNAPSHOT (FGC_BEFORE + 16), 0, 4, 0x4c, 0x150
    FG_ERRORS FGC_ERRORS_BEFORE
    movi a6, 1
    s32i a6, a2, FGC_SNAPSHOTS
    l32i a7, a2, FGC_ERRORS_BEFORE
    FG_FAULT_STATUS a7, a8
    FG_BNEZ a7, .Lprecondition
    l32i a7, a2, (FGC_ERRORS_BEFORE + 8)
    FG_BNEZ a7, .Lprecondition
    l32i a7, a2, (FGC_ERRORS_BEFORE + 4)
    beqz a7, .Lerrors_clean
    /* Mode 3 alone may acknowledge the captured CPU0 IBUS_WR_ICACHE
     * latch. Preserve its original value and read back all three status
     * words. Pinned S3 cache_ll clears this latch through W1C at +0xec.
     * Neither other bits nor an immediately recurring error are accepted.
     */
    l32i a8, a2, FGC_ACK_REQUEST
    bbsi a8, 0, .Lack_requested
    j .Lprecondition
.Lack_requested:
    movi a8, 2
    beq a7, a8, .Lack_known
    j .Lprecondition
.Lack_known:
    s32i a8, a2, FGC_ACKNOWLEDGED
    FG_WRITE a8, 0xec
    FG_ERRORS FGC_ERRORS_ACK
    l32i a7, a2, FGC_ERRORS_ACK
    FG_FAULT_STATUS a7, a8
    l32i a8, a2, (FGC_ERRORS_ACK + 4)
    or a7, a7, a8
    l32i a8, a2, (FGC_ERRORS_ACK + 8)
    or a7, a7, a8
    FG_BNEZ a7, .Lprecondition
.Lerrors_clean:
    FG_PREFLIGHT FGC_BEFORE, 0xa0, 0x94, 0x154
    FG_PREFLIGHT (FGC_BEFORE + 16), 0x4c, 0x40, 0x150
    l32i a7, a2, FGC_ABORT
    FG_BNEZ a7, .Lpreflight_timeout
    movi a6, 1
    s32i a6, a2, FGC_ATTEMPT
    s32i a6, a2, FGC_MUTATED
    movi a6, 2
    s32i a6, a2, FGC_STAGE
    memw
    FG_SUSPEND FGC_BEFORE, 0x60, 0x64, 0xa0, 0x94, 0x154, 0
    movi a6, 3
    s32i a6, a2, FGC_MUTATED
    FG_SUSPEND (FGC_BEFORE + 16), 0, 4, 0x4c, 0x40, 0x150, 12
    FG_READ a7, 0x60
    s32i a7, a2, FGC_SUSPENDED
    FG_READ a7, 0
    s32i a7, a2, (FGC_SUSPENDED + 4)
    FG_READ a7, 0x64
    s32i a7, a2, (FGC_SUSPENDED + 8)
    FG_READ a7, 4
    s32i a7, a2, (FGC_SUSPENDED + 12)
    FG_READ a7, 0x130
    s32i a7, a2, FGC_STATE_OFF
    movi a6, 3
    s32i a6, a2, FGC_SNAPSHOTS
    movi a6, 3
    s32i a6, a2, FGC_STAGE
    memw
    rsr a13, ccount
    s32i a13, a1, 16
.Lhold:
    l32i a7, a2, FGC_PEER_OFF
    bnez a7, .Lhold_minimum
    l32i a7, a2, FGC_ABORT
    bnez a7, .Lrestore_timeout
    rsr a11, ccount
    sub a11, a11, a13
    movi a8, 240
    slli a8, a8, 10
    bltu a11, a8, .Lhold
    j .Lrestore_timeout
.Lhold_minimum:
    rsr a11, ccount
    sub a11, a11, a13
    movi a8, 300
    slli a8, a8, 4
    bltu a11, a8, .Lhold_minimum
    s32i a11, a2, FGC_HOLD
#ifdef CONFIG_MTD_ESP32S3_NOR
    l32i a7, a2, FGC_NOR_ENTRY
    beqz a7, .Lnative_done
    /* A call0 callee owns all general registers. Preserve every live
     * guard value in this internal frame, including the control pointer. */
    s32i a2, a1, 20
    s32i a3, a1, 24
    s32i a4, a1, 28
    s32i a12, a1, 32
    s32i a14, a1, 36
    l32i a2, a2, FGC_NOR_TASK
    callx0 a7
    l32i a2, a1, 20
    l32i a3, a1, 24
    l32i a4, a1, 28
    l32i a12, a1, 32
    l32i a14, a1, 36
    l32i a8, a2, FGC_NOR_TASK
    l32i a7, a8, 16
    s32i a7, a1, 12
    l32i a13, a1, 16
    rsr a11, ccount
    sub a11, a11, a13
    s32i a11, a2, FGC_HOLD
.Lnative_done:
#endif
    j .Lrestore
.Lrestore_timeout:
    movi a6, 3
    s32i a6, a1, 12
.Lrestore:
    movi a6, 4
    s32i a6, a2, FGC_STAGE
    memw
    FG_RESTORE FGC_BEFORE, 0x60, 0x64, 0xa0, 0x94, 0x154
    l32i a7, a2, FGC_MUTATED
    bbsi a7, 1, .Ld_restore
    j .Ld_unchanged
.Ld_restore:
    FG_RESTORE (FGC_BEFORE + 16), 0, 4, 0x4c, 0x40, 0x150
.Ld_unchanged:
    /* Readback alone cannot prove that accesses will resume. Both
     * controllers must also reach IDLE before either CPU is released.
     */
    FG_POLL 0x130, 0, 12, 1, .Lfatal
    FG_POLL 0x130, 12, 12, 1, .Lfatal
    FG_SNAPSHOT FGC_AFTER, 0x60, 0x64, 0xa0, 0x154
    FG_SNAPSHOT (FGC_AFTER + 16), 0, 4, 0x4c, 0x150
    FG_COMPARE FGC_BEFORE, FGC_AFTER, -1
    FG_COMPARE (FGC_BEFORE + 4), (FGC_AFTER + 4), -1
    FG_COMPARE (FGC_BEFORE + 8), (FGC_AFTER + 8), -9
    FG_COMPARE (FGC_BEFORE + 12), (FGC_AFTER + 12), -5
    FG_COMPARE (FGC_BEFORE + 16), (FGC_AFTER + 16), -1
    FG_COMPARE (FGC_BEFORE + 20), (FGC_AFTER + 20), -1
    FG_COMPARE (FGC_BEFORE + 24), (FGC_AFTER + 24), -9
    FG_COMPARE (FGC_BEFORE + 28), (FGC_AFTER + 28), -5
    FG_ERRORS FGC_ERRORS_AFTER
    l32i a6, a2, FGC_SNAPSHOTS
    movi a7, 4
    or a6, a6, a7
    s32i a6, a2, FGC_SNAPSHOTS
    movi a6, 5
    s32i a6, a2, FGC_STAGE
    memw
    j .Lmaster_final
.Lprecondition:
    movi a6, 5
    s32i a6, a1, 12
    j .Lmaster_final
.Lpreflight_timeout:
    movi a6, 3
    s32i a6, a1, 12
.Lmaster_final:
    l32i a6, a1, 12
    bnez a6, .Lpublish_final
    l32i a7, a2, FGC_ABORT
    beqz a7, .Lsuccess
    movi a6, 3
    j .Lpublish_final
.Lsuccess:
    movi a6, 1
.Lpublish_final:
    s32i a6, a2, FGC_FINAL
    memw
    j .Lfinish
.Lpeer:
    l32i a6, a2, FGC_FINAL
    bnez a6, .Lfinish
    l32i a7, a2, FGC_STAGE
    movi a8, 3
    bne a7, a8, .Lpeer_timeout
    movi a7, 1
    s32i a7, a2, FGC_PEER_OFF
    memw
.Lpeer_timeout:
    rsr a11, ccount
    sub a11, a11, a12
    bltu a11, a5, .Lpeer
    movi a7, 1
    s32i a7, a2, FGC_ABORT
    memw
    j .Lpeer
.Lfinish:
    movi a7, 1
    bne a6, a7, .Lstatus
    movi a6, 0
.Lstatus:
    s32i a6, a14, FGC_RESULT
    rsr a11, ccount
    sub a11, a11, a12
    s32i a11, a14, FGC_ELAPSED
    memw
    l32i a0, a1, 4
    l32i a8, a1, 0
    mov a1, a8
    mov a2, a6
    retw
.Lfatal:
    movi a6, 7
    s32i a6, a2, FGC_STAGE
    memw
.Lfatal_wait:
    j .Lfatal_wait

esp32_flash_cache_template_end:
    .end no-transform
    .size esp32_flash_cache_template_start, .-esp32_flash_cache_template_start
