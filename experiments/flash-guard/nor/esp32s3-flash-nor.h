/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef ESP32S3_FLASH_NOR_H
#define ESP32S3_FLASH_NOR_H

/* Additional SRAM1, immediately below the already tested guard. */
#define FN_BASE 0x3fcc7000
#define FN_BYTES 0x1000
#define FN_TASK 0xf00
#define FN_FIFO_BYTES 64
#define FN_TIMEOUT_CYCLES 1200000000U
#define FN_COMMAND_CYCLES 240000U
#define FN_SCRATCH 0x00ff0000U
#define FN_ERASE_BYTES 0x10000U
#define FN_ETC_BASE 0x000d0000U
#define FN_ETC_BYTES 0x00070000U
#define FN_HOME_BASE 0x00c00000U
#define FN_HOME_BYTES 0x00400000U
#define FN_IDENTIFY 1U
#define FN_READ 2U
#define FN_PROGRAM 3U
#define FN_ERASE 4U
/* Identical 24-bit/256-byte/64-KiB command path in pinned IDF. */
#define FN_JEDEC_SUPPORTED(id) ((id) == 0xef4018U || (id) == 0xc84018U)

/* Independent of kernel headers: the cache-off object uses call0 ABI. */
typedef __UINT32_TYPE__ fn_u32;
struct fn_task {
	fn_u32 operation, address, length, timeout;
	fn_u32 result, jedec, status_before, status_after;
	fn_u32 commands, changed, cache_syncs, restored;
	fn_u32 data[FN_FIFO_BYTES / 4];
	/* First rejected hardware precondition, captured with both caches off. */
	fn_u32 preflight_register, preflight_value;
};

#endif
