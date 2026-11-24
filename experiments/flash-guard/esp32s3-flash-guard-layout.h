/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef ESP32S3_FLASH_GUARD_LAYOUT_H
#define ESP32S3_FLASH_GUARD_LAYOUT_H

#define FG_BASE 0x3fcc8000
#define FG_BYTES 0x2000
#define FG_ALIAS 0x6f0000
#define FG_CACHE_CODE 0x100
#define FG_CONTROL 0xe00
#define FG_STACK(cpu) (0x1000 + (cpu) * 0x800)
#define FG_STACK_BYTES 0x800
#define FG_CANARY 0x9b21a55a
#define FG_RENDEZVOUS_CYCLES 12000000
#define FG_POLL_CYCLES 245760
#define FG_HOLD_CYCLES 4800
#define FG_IRQ_SOURCES 99

/* EXTMEM +0xe4 bits 7..11 report access/miss counter overflow, not faults. */
#define FG_CACHE_COUNTER_STATUS_MASK 0x00000f80

/* First eleven words retain the hardware-tested dry-run ABI. */
#define FGC_READY 0
#define FGC_FINAL 8
#define FGC_RESULT 12
#define FGC_SP 20
#define FGC_PC 28
#define FGC_ELAPSED 36
#define FGC_ABORT 44
#define FGC_ATTEMPT 48
#define FGC_STAGE 52
#define FGC_MUTATED 56
#define FGC_HOLD 60
#define FGC_IRQ_OFF 64
#define FGC_IRQ_SAVED 72
#define FGC_PS 80
#define FGC_NMI 88
#define FGC_WINDOWS 96
#define FGC_BEFORE 104
#define FGC_AFTER 136
#define FGC_ERRORS_BEFORE 168
#define FGC_ERRORS_AFTER 180
#define FGC_SUSPENDED 192
#define FGC_STATE_OFF 208
#define FGC_PEER_OFF 212
#define FGC_ACK_REQUEST 216
#define FGC_ERRORS_ACK 220
#define FGC_ACKNOWLEDGED 232
#define FGC_SNAPSHOTS 236
#define FGC_NOR_TASK 240
#define FGC_NOR_ENTRY 244
#define FGC_BYTES 248

#ifndef __ASSEMBLER__
static inline u32 fg_cache_fault_status(unsigned int word, u32 status)
{
	/* Keep real faults, unknown bits and both CPUs' access-error words. */
	return word ? status : status & ~FG_CACHE_COUNTER_STATUS_MASK;
}

struct fg_control {
	u32 ready[2], release, result[2], sp[2], pc[2], elapsed[2];
	u32 abort_request, attempt, stage, mutated, hold_cycles;
	u32 irq_off[2], irq_saved[2], ps[2], nmi[2], windows[2];
	u32 before[8], after[8], errors_before[3], errors_after[3];
	u32 suspended[4], state_off, peer_off;
	u32 ack_request, errors_ack[3], acknowledged;
	u32 snapshots;
	u32 nor_task, nor_entry;
};
#endif
#endif
