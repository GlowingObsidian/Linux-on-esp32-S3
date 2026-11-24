/* SPDX-License-Identifier: GPL-2.0-only */
/* Native SPI1 transactions, derived from the pinned ESP32-S3 SPIMEM LL.
 * Compiled with call0 ABI, text-section literals and no external calls.
 * Only this object executes while flash is busy. Its argument, code,
 * constants and stack must all reside in explicitly owned SRAM1.
 */
#include "esp32s3-flash-nor.h"

#define INLINE static __attribute__((always_inline)) inline
#define SPI1 0x60002000U
#define SPI0 0x60003000U
#define EXTMEM 0x600c4000U
#define USR (1U << 18)
#define FLASH_PE (1U << 17)
#define D_Q_HIGH ((1U << 19) | (1U << 18))
#define COMMAND (1U << 31)
#define ADDRESS (1U << 30)
#define MISO (1U << 28)
#define MOSI (1U << 27)
#define CS_HOLD_SETUP ((1U << 6) | (1U << 7))
/* Preserve the existing rejection checks while reporting the exact register.
 * The task resides in SRAM; no diagnostic helper or log runs with caches off. */
#define CHECK_REGISTER(reg, reject) do { \
	t->preflight_register = (reg); \
	t->preflight_value = rd(reg); \
	if (reject) \
		return; \
} while (0)

INLINE fn_u32 rd(fn_u32 address)
{
	fn_u32 value;
	__asm__ volatile("memw" ::: "memory");
	value = *(volatile fn_u32 *)(__UINTPTR_TYPE__)address;
	__asm__ volatile("memw" ::: "memory");
	return value;
}

INLINE void wr(fn_u32 address, fn_u32 value)
{
	__asm__ volatile("memw" ::: "memory");
	*(volatile fn_u32 *)(__UINTPTR_TYPE__)address = value;
	__asm__ volatile("memw" ::: "memory");
}

INLINE fn_u32 cycles(void)
{
	fn_u32 value;
	__asm__ volatile("rsr %0, ccount" : "=a"(value));
	return value;
}

INLINE void park(void)
{
	/* A busy controller/flash cannot safely release either CPU into XIP. */
	for (;;)
		__asm__ volatile("memw" ::: "memory");
}

INLINE fn_u32 transact(struct fn_task *t, fn_u32 command,
			 fn_u32 address_bits, fn_u32 input, fn_u32 output)
{
	fn_u32 start = cycles();
	fn_u32 user = COMMAND | CS_HOLD_SETUP;
	if (address_bits)
		user |= ADDRESS;
	if (input)
		user |= MISO;
	if (output)
		user |= MOSI;
	wr(SPI1 + 0x18, user);
	wr(SPI1 + 0x1c, address_bits ? (address_bits - 1) << 26 : 0);
	wr(SPI1 + 0x20, (7U << 28) | command);
	wr(SPI1 + 0x24, output ? output * 8 - 1 : 0);
	wr(SPI1 + 0x28, input ? input * 8 - 1 : 0);
	/* SPI_MEM_FLASH_PE marks user-mode program/erase, as in NuttX. Both
	 * command indicators are self-clearing; poll the user transaction bit. */
	wr(SPI1, USR | ((command == 0x02 || command == 0xd8) ? FLASH_PE : 0));
	t->commands++;
	while (rd(SPI1) & USR) {
		if ((fn_u32)(cycles() - start) >= FN_COMMAND_CYCLES) {
			t->result = 3;
			park();
		}
	}
	return rd(SPI1 + 0x58);
}

INLINE fn_u32 status(struct fn_task *t)
{
	return transact(t, 0x05, 0, 1, 0) & 0xff;
}

INLINE void wait_ready(struct fn_task *t, fn_u32 start)
{
	while (status(t) & 1) {
		if ((fn_u32)(cycles() - start) >= t->timeout) {
			t->result = 3;
			park();
		}
	}
}

INLINE int allowed(fn_u32 address, fn_u32 length)
{
	return length &&
		((address >= FN_ETC_BASE && address < FN_ETC_BASE + FN_ETC_BYTES &&
		  length <= FN_ETC_BASE + FN_ETC_BYTES - address) ||
		 (address >= FN_HOME_BASE && address < FN_HOME_BASE + FN_HOME_BYTES &&
		  length <= FN_HOME_BASE + FN_HOME_BYTES - address));
}

INLINE fn_u32 saved_offset(fn_u32 i)
{
	if (i < 2)
		return 4 + i * 4;
	if (i < 9)
		return (i + 2) * 4;
	if (i < 11)
		return 0x34 + (i - 9) * 8;
	return i == 11 ? 0xa8 : 0xe0;
}

INLINE void invalidate(struct fn_task *t, fn_u32 ctrl, fn_u32 address,
			fn_u32 bytes, fn_u32 line_shift, fn_u32 done)
{
	fn_u32 addr_reg = EXTMEM + ctrl + 4;
	fn_u32 size_reg = EXTMEM + ctrl + 8;
	fn_u32 old_addr = rd(addr_reg), old_size = rd(size_reg);
	fn_u32 mask = (1U << line_shift) - 1;
	fn_u32 count = (bytes + (address & mask) + mask) >> line_shift;
	fn_u32 start = cycles();
	wr(addr_reg, address & ~mask);
	wr(size_reg, count);
	wr(EXTMEM + ctrl, 1);
	while (!(rd(EXTMEM + ctrl) & done)) {
		if ((fn_u32)(cycles() - start) >= FN_COMMAND_CYCLES) {
			t->result = 3;
			park();
		}
	}
	wr(EXTMEM + ctrl, 0);
	wr(addr_reg, old_addr);
	wr(size_reg, old_size);
	t->cache_syncs++;
}

/* Labels bracket the literal pool as well as the function. Verify the
 * final linked range and every literal/call before copying it to SRAM. */
__asm__(".section .text.esp32s3_nor_template,\"ax\",@progbits\n"
	".balign 4\n.global esp32s3_nor_blob_start\n"
	"esp32s3_nor_blob_start:\n.previous\n.begin no-transform\n");

void esp32s3_nor_entry(struct fn_task *t);

__attribute__((__noinline__, __section__(".text.esp32s3_nor_template")))
void esp32s3_nor_entry(struct fn_task *t)
{
	/* Keep the preceding literal pool private: Xtensa ld coalesces literals
	 * unless their property entries carry NO_TRANSFORM. Normal instruction
	 * relaxation may resume after the compiler has emitted this pool. */
	__asm__ volatile(".begin transform\n" ::: "memory");
	/* All saved registers are on the caller's internal call0 stack. */
	fn_u32 save[13], i, start, sr, sr2;
	t->result = 5;
	if (t->operation < FN_IDENTIFY || t->operation > FN_ERASE ||
	    !t->timeout || t->timeout > FN_TIMEOUT_CYCLES)
		return;
	if (t->operation != FN_IDENTIFY &&
	    (!allowed(t->address, t->operation == FN_ERASE ? FN_ERASE_BYTES : t->length) ||
	     (t->operation == FN_ERASE ? (t->address & (FN_ERASE_BYTES - 1)) :
	      (!t->length || t->length > FN_FIFO_BYTES ||
	       (t->operation == FN_PROGRAM && (t->address & 255) + t->length > 256)))))
		return;
	/* This port uses single-command STR flash, CS0, with octal PSRAM on
	 * SPI0. Reject unsupported flash DDR/OPI instead of changing chip mode. */
	CHECK_REGISTER(SPI1, t->preflight_value);
	CHECK_REGISTER(SPI0, t->preflight_value);
	CHECK_REGISTER(SPI0 + 0x54, t->preflight_value & 7);
	CHECK_REGISTER(SPI1 + 0x54, t->preflight_value & 7);
	CHECK_REGISTER(SPI0 + 0xe0, t->preflight_value & 1);
	CHECK_REGISTER(SPI0 + 0x08, t->preflight_value & 0x3f0U);
	CHECK_REGISTER(SPI1 + 0xa0, t->preflight_value & 1);
	CHECK_REGISTER(SPI1 + 0x98, t->preflight_value & 1);
	CHECK_REGISTER(SPI1 + 0xa4, t->preflight_value & 1);
	/* CLOCK_GATE is register-clock gating, not the MSPI functional clock.
	 * NuttX's SPI1 transaction setup explicitly writes zero here. Both zero
	 * and one are valid; leave the inherited register unchanged. */
	CHECK_REGISTER(EXTMEM + 0x88, t->preflight_value & 1);
	CHECK_REGISTER(EXTMEM + 0x28, t->preflight_value & 7);
	CHECK_REGISTER(EXTMEM, ((t->preflight_value >> 3) & 3) > 2);
	t->preflight_register = 0;
	t->preflight_value = 0;
	for (i = 0; i < 13; i++)
		save[i] = rd(SPI1 + saved_offset(i));
	/* SPI1 only. Leave SPI0 clock, PSRAM timing, pins and cache MMU alone. */
	/* NuttX sets D/Q high, and IDF's dummy-output setup uses out_level=1.
	 * Keep every multi-line/read-mode flag off for these raw commands. */
	wr(SPI1 + 0x08, D_Q_HIGH);
	wr(SPI1 + 0x10, (save[2] & ~0x3fffU) | 1U | (1U << 7));
	wr(SPI1 + 0x14, 23U | (11U << 8) | (23U << 16));
	wr(SPI1 + 0x34, (save[9] & ~0xc03U) | 2U);
	wr(SPI1 + 0x3c, save[10] & 1U);
	wr(SPI1 + 0xa8, save[11] & ~(7U << 2));
	wr(SPI1 + 0xe0, save[12] & ~1U);
	start = cycles();
	wait_ready(t, start);
	sr = status(t);
	t->status_before = sr;
	if (sr & 2)
		goto restore;
	if (t->operation == FN_IDENTIFY) {
		fn_u32 id = transact(t, 0x9f, 0, 3, 0);
		fn_u32 manufacturer, capacity;
		__asm__ volatile("extui %0, %1, 0, 8" : "=a"(manufacturer) : "a"(id));
		__asm__ volatile("extui %0, %1, 16, 8" : "=a"(capacity) : "a"(id));
		t->jedec = (manufacturer << 16) | (id & 0xff00) | capacity;
		t->result = 0;
		goto restore;
	}
	if (!FN_JEDEC_SUPPORTED(t->jedec))
		goto restore;
	if (t->operation == FN_READ) {
		wr(SPI1 + 0x04, t->address);
		transact(t, 0x03, 24, t->length, 0);
		for (i = 0; i < (t->length + 3) / 4; i++)
			t->data[i] = rd(SPI1 + 0x58 + i * 4);
		t->result = 0;
		goto restore;
	}
	sr2 = transact(t, 0x35, 0, 1, 0) & 0xff;
	/* Never clear protection bits or change QE/calibration configuration. */
	if ((sr & 0x7c) || (sr2 & 0xc0))
		goto restore;
	transact(t, 0x06, 0, 0, 0);
	if ((status(t) & 3) != 2)
		goto disable;
	wr(SPI1 + 0x04, t->address);
	if (t->operation == FN_PROGRAM) {
		for (i = 0; i < (t->length + 3) / 4; i++)
			wr(SPI1 + 0x58 + i * 4, t->data[i]);
		t->changed = 1;
		transact(t, 0x02, 24, 0, t->length);
	} else {
		t->changed = 1;
		transact(t, 0xd8, 24, 0, 0);
	}
	start = cycles();
	wait_ready(t, start);
	t->status_after = status(t);
	if (!(t->status_after & 3))
		t->result = 0;
disable:
	transact(t, 0x04, 0, 0, 0);
	if (status(t) & 3) {
		t->result = 6;
		park();
	}
restore:
	if (t->changed) {
		fn_u32 bytes = t->operation == FN_ERASE ? FN_ERASE_BYTES : t->length;
		/* Public S3 register definitions use cache-line counts. Invalidate
		 * only the two flash aliases; preserve every dirty PSRAM line. */
		invalidate(t, 0x88, 0x42000000U + t->address, bytes,
			   4 + ((rd(EXTMEM + 0x60) >> 3) & 1), 2);
		invalidate(t, 0x28, 0x3c000000U + t->address, bytes,
			   4 + ((rd(EXTMEM) >> 3) & 3), 8);
	}
	for (i = 0; i < 13; i++)
		wr(SPI1 + saved_offset(i), save[i]);
	for (i = 0; i < 13; i++) {
		if (rd(SPI1 + saved_offset(i)) != save[i]) {
			t->result = 6;
			park();
		}
	}
	t->restored = 1;
}

__asm__(".end transform\n.end no-transform\n"
	".section .text.esp32s3_nor_template,\"ax\",@progbits\n"
	".global esp32s3_nor_blob_end\nesp32s3_nor_blob_end:\n.previous\n");
