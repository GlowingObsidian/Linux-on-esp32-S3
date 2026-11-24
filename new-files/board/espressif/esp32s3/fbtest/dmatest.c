/*
 * dmatest -- does GDMA actually move bytes, and does the cache lie about them.
 *
 * Why this program exists
 * -----------------------
 * Three things about GDMA on this board are already known, all from the kernel
 * probe in patch 0070:
 *
 *   1. Linux can reach the GDMA register block. GDMA_DATE reads 0x02101180,
 *      which is its documented value.
 *   2. Linux can reach the internal SRAM at 0x3FC88000, which is where GDMA
 *      requires its descriptor list to live (TRM 3.4.1).
 *   3. SPI2 can be selected as a GDMA trigger.
 *
 * What is not known is the only question that matters: when the engine is
 * told to move bytes, do the bytes arrive? Everything above is a precondition.
 * This program is the transfer.
 *
 * The reason it cannot be answered by reading
 * -------------------------------------------
 * This kernel is built with XCHAL_DCACHE_SIZE 0, so every arch cache
 * primitive is a no-op and nothing in the kernel ever flushes a data cache.
 * The silicon, however, has a real 32 KiB write-back L1 sitting over PSRAM.
 * So the CPU can write a descriptor, the write can sit in that cache, and
 * GDMA -- which does not read through the CPU's cache -- can fetch whatever
 * was there before. That is not a hypothetical failure mode; it is the
 * classic one, and it is invisible to every check the kernel could run on
 * itself.
 *
 * So this program writes a known pattern, starts the engine, waits for it to
 * finish, and compares. If the pattern arrives, the cache is not in the way
 * for this path and the SPI integration can be built. If it does not, the
 * fix is a cache flush at the right point, which is a known and bounded piece
 * of work -- and this program will have said exactly which point.
 *
 * What it does, in order
 * ----------------------
 * The descriptor list and both buffers live in the internal SRAM window. That
 * is deliberate: it is the placement the real driver will use, so a pass here
 * is evidence about the real thing rather than about a convenient case.
 *
 *   A. mmap the SRAM window. If this fails, nothing else can be tested and
 *      the failure is the device's, not the engine's.
 *   B. Read a word, write a word, read it back. This is CPU-to-CPU and tests
 *      the mapping, not the engine -- if it fails, stop, because a transfer
 *      failure on top of a broken mapping would be misread as a cache
 *      problem.
 *   C. Memory-to-memory: source filled with a pattern, channel told to copy
 *      it to a destination, engine started, done bit polled, destination
 *      compared. This is the cache question, with no peripheral involved.
 *
 * C is the whole test for now. A second pass with the buffers in PSRAM rather
 * than internal SRAM is the obvious follow-up -- that is the case the cache
 * would break first, since internal SRAM is not behind it -- but it is a
 * separate question and a separate program, and it needs a physical address
 * for a PSRAM buffer that userspace has no way to obtain on NOMMU. Internal
 * SRAM is also the placement the real driver will use, so C is the one that
 * has to pass before anything else is worth measuring.
 *
 * Safety
 * ------
 * This writes into internal SRAM that Linux does not own. The window is
 * 0x3FC88000-0x3FCFFFFF, which the device tree does not describe as memory,
 * so the kernel is not using it -- but core 0's firmware might be. The
 * program confines itself to the last 4 KB of the window, well away from the
 * base, and it says so before it writes. It also restores what it found.
 *
 * Build: see the header of fbtest.c. Same toolchain, same flags.
 */

#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>

#define PAGE_SIZE	4096

/* The device the register window lives on. */
#define REGS_DEV	"/dev/esp32-regs"

/* The mmap window: internal SRAM, the only place a descriptor may live. */
#define SRAM_BASE	0x3FC88000UL
#define SRAM_SIZE	0x78000UL
#define SRAM_END	(SRAM_BASE + SRAM_SIZE)

/*
 * Where in the window this program works. The last 4 KB, so a mistake cannot
 * land on whatever core 0 keeps at the base.
 */
#define WORK_OFF	(SRAM_SIZE - PAGE_SIZE)
#define WORK_BASE	(SRAM_BASE + WORK_OFF)

/*
 * GDMA, from the S3's own gdma_reg.h. These are channel-0 *offsets*, and
 * every use below adds GDMA_BASE. The first version of this file passed the
 * bare offsets to the device, which correctly refused them: 0x00000000 is
 * not inside any window the device hands out, so every register write came
 * back EINVAL and the engine was never configured at all.
 */
#define GDMA_BASE	0x6003F000UL

#define GDMA_IN_CONF0_CH0	(GDMA_BASE + 0x000)
#define GDMA_IN_INT_RAW_CH0	(GDMA_BASE + 0x008)
#define GDMA_IN_INT_CLR_CH0	(GDMA_BASE + 0x014)
#define GDMA_IN_LINK_CH0	(GDMA_BASE + 0x020)
#define GDMA_OUT_CONF0_CH0	(GDMA_BASE + 0x060)
#define GDMA_OUT_INT_RAW_CH0	(GDMA_BASE + 0x068)
#define GDMA_OUT_INT_CLR_CH0	(GDMA_BASE + 0x074)
#define GDMA_OUT_LINK_CH0	(GDMA_BASE + 0x080)

/*
 * IN_CONF0. GDMA_MEM_TRANS_EN_CH0 is bit 4 of the IN channel only -- there is
 * no OUT_MEM_TRANS_EN_CH0 in the S3's register map. Bit 4 of OUT_CONF0 is
 * GDMA_OUTDSCR_BURST_EN_CH0, the descriptor-burst enable, which is a
 * different thing entirely. The first version of this file defined an
 * OUT_MEM_TRANS_EN and set that bit, which would have enabled descriptor
 * bursting instead of memory-to-memory and left the transfer waiting for a
 * peripheral that was never going to arrive.
 */
#define IN_RST			(1u << 0)
#define IN_MEM_TRANS_EN		(1u << 4)
/* OUT_CONF0 */
#define OUT_RST			(1u << 0)

/* IN_LINK */
#define INLINK_START		(1u << 22)
#define INLINK_ADDR_MASK	0x000FFFFF
/* OUT_LINK */
#define OUTLINK_START		(1u << 21)
#define OUTLINK_ADDR_MASK	0x000FFFFF

/* Interrupt raw bits that mean the transfer finished or failed. */
#define IN_DONE			(1u << 0)
#define IN_SUC_EOF		(1u << 1)
#define IN_DSCR_ERR		(1u << 3)
#define OUT_DONE		(1u << 0)
#define OUT_EOF			(1u << 1)
#define OUT_DSCR_ERR		(1u << 2)

/*
 * The descriptor. 12 bytes, and the layout is not negotiable -- it is what the
 * engine reads. Taken from IDF's dma_types.h, which asserts the same 12 bytes.
 *
 * The bitfield order matters and is compiler-dependent, so the word is built
 * by hand rather than declared as a bitfield. Getting this wrong is the
 * failure that looks like "the engine did nothing".
 */
struct gdma_desc {
	uint32_t dw0;
	uint32_t buffer;
	uint32_t next;
} __attribute__((aligned(4)));

#define DW0_SIZE(n)	((uint32_t)((n) & 0xFFF))
#define DW0_LENGTH(n)	((uint32_t)(((n) & 0xFFF) << 12))
#define DW0_SUC_EOF	(1u << 30)
#define DW0_OWNER	(1u << 31)

/*
 * No initializer, deliberately. This is the only file-scope variable in the
 * program that would otherwise need one, and an initialized global is what
 * makes the linker emit a .data section -- which this Xtensa linker script
 * places at a vaddr that is not congruent with its file offset modulo the
 * page size. uclibc's loader maps that segment with mmap, mmap rejects the
 * misalignment with EINVAL, and the program segfaults before main() with no
 * output at all. fbtest has no initialized globals, which is why fbtest runs
 * and this did not.
 *
 * 0 is not a valid descriptor here, so it is a safe "not open" value; every
 * path below sets it before use, and the error paths set it back to -1.
 */
static int regs_fd;
static volatile uint8_t *sram;

static int failures;

static void ok(int cond, const char *what)
{
	if (cond) {
		printf("ok:   %s\n", what);
	} else {
		failures++;
		printf("FAIL: %s\n", what);
	}
	fflush(stdout);
}

/* Read a word from the SRAM window at a physical address. */
static uint32_t sram_rd(uint32_t phys)
{
	return *(volatile uint32_t *)(sram + (phys - SRAM_BASE));
}

static void sram_wr(uint32_t phys, uint32_t val)
{
	*(volatile uint32_t *)(sram + (phys - SRAM_BASE)) = val;
}

/*
 * Open the register window and mmap the SRAM window.
 *
 * MAP_SHARED is required, not preferred. On NOMMU, MAP_PRIVATE takes the copy
 * path in mm/nommu.c and hands back a private copy of the window -- writes
 * would go to the copy, the engine would read the original, and every test
 * below would fail for a reason that has nothing to do with DMA.
 */
static int open_windows(void)
{
	off_t off;
	void *p;

	regs_fd = open(REGS_DEV, O_RDWR);
	if (regs_fd < 0) {
		printf("FAIL: open(%s): %s\n", REGS_DEV, strerror(errno));
		printf("      is CONFIG_ESP32_REGS on in this build?\n");
		return -1;
	}

	/*
	 * mmap needs a page offset, and the window base is page-aligned, so
	 * the whole window is offset 0.
	 */
	off = 0;
	p = mmap(NULL, SRAM_SIZE, PROT_READ | PROT_WRITE, MAP_SHARED,
		 regs_fd, off);
	if (p == MAP_FAILED) {
		printf("FAIL: mmap(%lu bytes of SRAM): %s\n",
		       (unsigned long)SRAM_SIZE, strerror(errno));
		printf("      the device must report NOMMU_MAP_DIRECT and a"
		       " get_unmapped_area hook\n");
		close(regs_fd);
		regs_fd = -1;
		return -1;
	}

	sram = (volatile uint8_t *)p;
	printf("info: SRAM window mapped at %p, asked for physical 0x%08lx\n",
	       p, (unsigned long)SRAM_BASE);

	/*
	 * On NOMMU the address a program gets IS the physical address. If
	 * these differ, the mapping is not the window and every address below
	 * is wrong.
	 */
	ok((unsigned long)p == SRAM_BASE,
	   "the mapping is at the physical address, as NOMMU requires");

	/* The register window is reached through the same device, by read(). */
	return 0;
}

/*
 * Reach the GDMA registers.
 *
 * The device's mmap window is SRAM and only SRAM, deliberately -- a register
 * window handed to userspace whole is a much bigger hammer than one register
 * at a time. So the registers are reached the way the device intends: a
 * command in through write(), the answer out through read().
 *
 * That is two syscalls per access, which is far too slow to poll a done bit
 * in a tight loop. It does not need to be: the transfer below is 256 bytes
 * and finishes in microseconds, so the first poll after the start already
 * sees the done bit set. The loop is a formality with a bound, not a spin.
 */
static int reg_write(uint32_t addr, uint32_t val)
{
	char cmd[64];
	ssize_t n;

	n = snprintf(cmd, sizeof(cmd), "write 0x%08x 0x%08x\n", addr, val);
	if (write(regs_fd, cmd, (size_t)n) != n) {
		printf("FAIL: register write 0x%08x: %s\n", addr,
		       strerror(errno));
		return -1;
	}
	return 0;
}

static int reg_read(uint32_t addr, uint32_t *out)
{
	char cmd[64];
	char buf[128];
	ssize_t n;
	unsigned long a;
	unsigned int v;

	n = snprintf(cmd, sizeof(cmd), "read 0x%08x\n", addr);
	if (write(regs_fd, cmd, (size_t)n) != n)
		return -1;

	/*
	 * The answer comes out of read(). The device clears it after handing
	 * it over, so each read gets one command's result and no other.
	 */
	n = read(regs_fd, buf, sizeof(buf) - 1);
	if (n <= 0)
		return -1;
	buf[n] = '\0';

	if (sscanf(buf, "0x%lx 0x%x", &a, &v) != 2)
		return -1;

	*out = (uint32_t)v;
	return 0;
}

/*
 * Run one memory-to-memory transfer and report what happened.
 *
 * src and dst are physical addresses; the engine is given physical addresses
 * because that is what it uses. len is bytes.
 */
static int run_transfer(uint32_t desc_phys, uint32_t src_phys,
			uint32_t dst_phys, uint32_t len, const char *label)
{
	uint32_t raw;
	int i;
	int started = 0;

	printf("\n--- %s ---\n", label);
	printf("info: desc=0x%08x src=0x%08x dst=0x%08x len=%u\n",
	       desc_phys, src_phys, dst_phys, len);

	/* Reset both channels and clear any stale interrupt. */
	reg_write(GDMA_IN_CONF0_CH0, IN_RST);
	reg_write(GDMA_OUT_CONF0_CH0, OUT_RST);
	reg_write(GDMA_IN_INT_CLR_CH0, 0xFFFFFFFF);
	reg_write(GDMA_OUT_INT_CLR_CH0, 0xFFFFFFFF);
	reg_write(GDMA_IN_CONF0_CH0, 0);
	reg_write(GDMA_OUT_CONF0_CH0, 0);

	/*
	 * Memory-to-memory. Without this bit the in channel expects a
	 * peripheral to feed it and will sit waiting for one. It is set on
	 * the in channel only -- see the IN_CONF0 note above.
	 */
	reg_write(GDMA_IN_CONF0_CH0, IN_MEM_TRANS_EN);
	reg_write(GDMA_OUT_CONF0_CH0, 0);

	/*
	 * The descriptor. owner=1 means "the engine may take this"; suc_eof
	 * means "this is the last one"; size is the buffer capacity and
	 * length is how much is valid.
	 */
	{
		uint32_t dw0 = DW0_SIZE(len) | DW0_LENGTH(len) |
			       DW0_SUC_EOF | DW0_OWNER;

		sram_wr(desc_phys + 0, dw0);
		sram_wr(desc_phys + 4, src_phys);
		sram_wr(desc_phys + 8, 0);
	}

	/*
	 * Point both channels at the descriptor. The low 20 bits are the
	 * address; the upper bits are control and must be written separately,
	 * or the START bit lands in the address field.
	 */
	reg_write(GDMA_IN_LINK_CH0, desc_phys & INLINK_ADDR_MASK);
	reg_write(GDMA_OUT_LINK_CH0, desc_phys & OUTLINK_ADDR_MASK);

	/* Start the out channel (it feeds the in channel in mem-to-mem). */
	reg_write(GDMA_OUT_LINK_CH0,
		  (desc_phys & OUTLINK_ADDR_MASK) | OUTLINK_START);
	reg_write(GDMA_IN_LINK_CH0,
		  (desc_phys & INLINK_ADDR_MASK) | INLINK_START);

	/* Poll for completion. */
	for (i = 0; i < 100000; i++) {
		if (reg_read(GDMA_IN_INT_RAW_CH0, &raw) < 0)
			break;
		if (raw & (IN_DONE | IN_SUC_EOF | IN_DSCR_ERR)) {
			started = 1;
			break;
		}
		if (reg_read(GDMA_OUT_INT_RAW_CH0, &raw) < 0)
			break;
		if (raw & (OUT_DONE | OUT_EOF | OUT_DSCR_ERR)) {
			started = 1;
			break;
		}
	}

	if (!started) {
		uint32_t in_raw = 0, out_raw = 0;

		printf("FAIL: the engine never reported completion\n");
		reg_read(GDMA_IN_INT_RAW_CH0, &in_raw);
		reg_read(GDMA_OUT_INT_RAW_CH0, &out_raw);
		printf("      in_int_raw=0x%08x out_int_raw=0x%08x\n",
		       in_raw, out_raw);
		failures++;
		return -1;
	}

	reg_read(GDMA_IN_INT_RAW_CH0, &raw);
	printf("info: in_int_raw=0x%08x\n", raw);
	ok(!(raw & IN_DSCR_ERR), "no descriptor error on the in channel");

	reg_read(GDMA_OUT_INT_RAW_CH0, &raw);
	printf("info: out_int_raw=0x%08x\n", raw);
	ok(!(raw & OUT_DSCR_ERR), "no descriptor error on the out channel");

	/* Stop and clean up. */
	reg_write(GDMA_IN_INT_CLR_CH0, 0xFFFFFFFF);
	reg_write(GDMA_OUT_INT_CLR_CH0, 0xFFFFFFFF);
	reg_write(GDMA_IN_CONF0_CH0, IN_RST);
	reg_write(GDMA_OUT_CONF0_CH0, OUT_RST);

	return 0;
}

/*
 * Fill a buffer with a pattern that is position-dependent, so a transfer that
 * lands at the wrong offset is caught rather than passing on a repeated byte.
 */
static void fill_pattern(uint32_t phys, uint32_t len, uint32_t seed)
{
	uint32_t i;

	for (i = 0; i < len; i += 4)
		sram_wr(phys + i, seed ^ (i * 2654435761u));
}

static int check_pattern(uint32_t phys, uint32_t len, uint32_t seed,
			 const char *what)
{
	uint32_t i;
	uint32_t bad = 0;
	uint32_t first_bad = 0;
	uint32_t got = 0, want = 0;

	for (i = 0; i < len; i += 4) {
		got = sram_rd(phys + i);
		want = seed ^ (i * 2654435761u);
		if (got != want) {
			if (!bad) {
				first_bad = i;
				/* keep got/want for the report */
			} else {
				/* recompute so the report shows the first */
			}
			bad++;
		}
	}

	if (bad) {
		got = sram_rd(phys + first_bad);
		want = seed ^ (first_bad * 2654435761u);
		printf("FAIL: %s: %u of %u words wrong; first at +%u"
		       " got 0x%08x want 0x%08x\n",
		       what, bad, len / 4, first_bad, got, want);
		failures++;
		return -1;
	}

	printf("ok:   %s: all %u words arrived intact\n", what, len / 4);
	return 0;
}

int main(int argc, char **argv)
{
	uint32_t save;
	uint32_t len = 256;
	uint32_t src = WORK_BASE + 0x000;
	uint32_t dst = WORK_BASE + 0x400;
	uint32_t desc = WORK_BASE + 0x800;
	int ret = 0;

	(void)argc;
	(void)argv;

	printf("dmatest: GDMA memory-to-memory on the ESP32-S3\n");
	printf("dmatest: this writes into internal SRAM Linux does not own.\n");
	printf("dmatest: confined to 0x%08lx-0x%08lx, the last 4 KB of the"
	       " window.\n", (unsigned long)WORK_BASE,
	       (unsigned long)(WORK_BASE + PAGE_SIZE - 1));
	fflush(stdout);

	if (open_windows() < 0)
		return 1;

	/* B. the mapping itself, CPU to CPU. */
	printf("\n--- the mapping ---\n");
	save = sram_rd(WORK_BASE);
	sram_wr(WORK_BASE, 0x5A5A5A5A);
	ok(sram_rd(WORK_BASE) == 0x5A5A5A5A,
	   "a word written through the mapping reads back");
	sram_wr(WORK_BASE, save);
	ok(sram_rd(WORK_BASE) == save,
	   "the original word was restored");

	/* The register window is reachable through the same device. */
	{
		uint32_t date = 0;

		if (reg_read(GDMA_BASE + 0x40C, &date) == 0) {
			printf("info: GDMA_DATE = 0x%08x\n", date);
			ok(date == 0x02101180,
			   "the GDMA date register reads its documented value");
		} else {
			ok(0, "the GDMA date register is readable");
		}
	}

	/* C. memory to memory, both buffers in internal SRAM. */
	fill_pattern(src, len, 0xDEADBEEF);
	fill_pattern(dst, len, 0x00000000);

	/*
	 * The source is read back before the transfer, through the CPU. If
	 * this is wrong the pattern never got written and a mismatch after
	 * the transfer would be blamed on the engine.
	 */
	ok(sram_rd(src) == (0xDEADBEEF ^ (0u * 2654435761u)),
	   "the source pattern is in memory before the transfer starts");

	if (run_transfer(desc, src, dst, len,
			 "memory to memory, internal SRAM") == 0)
		check_pattern(dst, len, 0xDEADBEEF,
			      "the pattern crossed internal SRAM");

	printf("\n");
	if (failures == 0) {
		printf("PASS: the engine moved bytes and the cache did not"
		       " interfere.\n");
		printf("      DMA is viable on this board for this path.\n");
	} else {
		printf("%d failure(s).\n", failures);
		printf("If the transfer completed but the data was wrong, the\n");
		printf("cache is the cause: this kernel never flushes one, and\n");
		printf("the silicon has a write-back L1 the kernel does not\n");
		printf("know about.\n");
		ret = 1;
	}

	if (sram)
		munmap((void *)sram, SRAM_SIZE);
	if (regs_fd >= 0)
		close(regs_fd);

	return ret;
}
