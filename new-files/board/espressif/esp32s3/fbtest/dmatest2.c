/*
 * dmatest2 -- does the DMA engine move bytes, asked through the kernel.
 *
 * Why this replaces dmatest
 * -------------------------
 * dmatest tried to answer this from userspace: mmap the internal SRAM window,
 * build the descriptor there, poke the GDMA registers through /dev/esp32-regs.
 * It cannot work, and mmprobe showed why. On this NOMMU kernel mmap of a
 * device window installs nothing -- remap_pfn_range() has no page table to
 * install a translation into, so it checks the address, sets VM_IO|VM_PFNMAP
 * and returns 0. The VMA is recorded, the address is returned, and the store
 * goes nowhere:
 *
 *   3fc88000-3fd00000 rw-S 00000000 00:07 99  /dev/esp32-regs
 *   wrote 0xa5c3e10d, read 0x00000000
 *   0xa5c3e10d appears nowhere else in the window
 *
 * That is not a bug in dmatest and not a bug in esp32-regs. The kernel reaches
 * the same address through ioremap() because ioremap() is the identity here
 * (CONFIG_MMU is off) and the kernel runs under the boot-time region TLB;
 * userspace does not get that. So the descriptor has to be built in the
 * kernel, and patch 0072 does exactly that.
 *
 * What this program is for
 * ------------------------
 * It is the userspace half of the proof. It hands the driver a source buffer
 * filled with a known pattern and asks for it to be copied through the engine
 * to a destination buffer, then compares. If the bytes arrive, the engine
 * moves data and the SPI integration can be built on it. If they do not, the
 * driver reports which stage failed and this program reports what it saw.
 *
 * The buffers are ordinary userspace memory -- the driver stages them through
 * internal SRAM itself, because that is the only memory the engine can reach
 * on this board. That staging is deliberate for this first version: it proves
 * the engine works without also having to prove a zero-copy mapping works.
 *
 * What it checks, in order
 * ------------------------
 *   A. The device exists and the engine is powered. A read of GDMA_DATE
 *      through the register window must return 0x02101180. If it reads zero
 *      the block is still gated and every register write below is accepted
 *      and ignored -- which is exactly the failure that made the first
 *      userspace attempt look like an addressing bug.
 *   B. A 4-byte transfer. The smallest thing the engine can do, so a failure
 *      here is about the engine and not about size.
 *   C. A 256-byte transfer, a whole number of words and well inside one
 *      descriptor's 12-bit size field.
 *   D. A 4096-byte transfer -- one page, the largest this version accepts,
 *      and the size the panel needs for a framebuffer row block.
 *   E. A transfer whose length is not a multiple of 4 must be refused rather
 *      than silently truncated.
 *   F. The destination is checked byte by byte, and the first mismatch is
 *      printed with both values, so a partial transfer says how far it got.
 *
 * 32-bit only, like everything in this directory: this toolchain's LRA pass
 * hits an internal compiler error lowering 64-bit values, so there is no
 * int64_t and no 64-bit arithmetic below.
 *
 * Build (in the build container, with the project's own toolchain):
 *
 *   xtensa-esp32s3-linux-uclibcfdpic-gcc -Os -mfdpic -mauto-litpools -fPIC \
 *       -ffunction-sections -fdata-sections -std=gnu17 -Wall -Wextra -Werror \
 *       -Wl,--gc-sections,-z,stack-size=65536 dmatest2.c -o dmatest2
 *
 * Same flags as fbtest, so it links against the same uclibc-fdpic.
 *
 * Usage:  dmatest2 [-v]
 *   -v   print a line per transfer, including the bytes moved
 */

#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <unistd.h>

#define GDMA_DEV	"/dev/esp32-gdma"
#define REGS_DEV	"/dev/esp32-regs"

/*
 * The ioctl, mirroring include/uapi/linux/esp32-gdma.h. Written out here
 * rather than included because this program is built by hand in the container
 * and the uapi header lives in the kernel tree, not in this directory.
 */
struct esp32_gdma_memcpy {
	uint64_t src;
	uint64_t dst;
	uint32_t len;
	uint32_t reserved;
};

#define ESP32_GDMA_MEMCPY	_IOW('G', 0x00, struct esp32_gdma_memcpy)

/* GDMA_DATE, the engine's only identity register. */
#define GDMA_BASE	0x6003F000UL
#define GDMA_DATE	(GDMA_BASE + 0x40C)
#define GDMA_DATE_VAL	0x02101180

#define MAX_XFER	4096

static int failures;
static int verbose;

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

/*
 * Read one word through /dev/esp32-regs, which goes through ioremap() in the
 * kernel. Used only for the GDMA_DATE check -- the transfer itself goes
 * through the DMA device.
 */
static int dev_read(int fd, uint32_t addr, uint32_t *out)
{
	char cmd[64];
	char buf[128];
	ssize_t n;
	unsigned long a;
	unsigned int v;

	n = snprintf(cmd, sizeof(cmd), "read 0x%08x\n", addr);
	if (write(fd, cmd, (size_t)n) != n)
		return -1;

	n = read(fd, buf, sizeof(buf) - 1);
	if (n <= 0)
		return -1;
	buf[n] = '\0';

	if (sscanf(buf, "0x%lx 0x%x", &a, &v) != 2)
		return -1;

	*out = (uint32_t)v;
	return 0;
}

/*
 * Fill a buffer with a pattern that changes every byte, so a transfer that
 * copies the right number of bytes from the wrong offset is visible, and so
 * a zeroed destination cannot accidentally match.
 */
static void fill_pattern(uint8_t *p, uint32_t len, uint32_t seed)
{
	uint32_t i;

	for (i = 0; i < len; i++)
		p[i] = (uint8_t)((i * 31u + seed) & 0xFF);
}

/*
 * Compare and report the first difference with both values, so a partial
 * transfer says how far it got rather than just that it failed.
 */
static void compare(const uint8_t *got, const uint8_t *want, uint32_t len,
		    const char *what)
{
	uint32_t i;

	for (i = 0; i < len; i++) {
		if (got[i] == want[i])
			continue;

		ok(0, what);
		printf("      first difference at byte %u: got 0x%02x,"
		       " expected 0x%02x\n", i, got[i], want[i]);
		if (i == 0)
			printf("      nothing was transferred at all\n");
		else
			printf("      the first %u byte(s) arrived\n", i);
		return;
	}

	ok(1, what);
}

/*
 * One transfer of len bytes. Returns 0 if the ioctl succeeded, so the caller
 * can distinguish "the engine ran and produced wrong bytes" from "the engine
 * never ran".
 */
static int transfer(int fd, const uint8_t *src, uint8_t *dst, uint32_t len,
		    const char *what)
{
	struct esp32_gdma_memcpy req;

	req.src = (uint64_t)(uintptr_t)src;
	req.dst = (uint64_t)(uintptr_t)dst;
	req.len = len;
	req.reserved = 0;

	if (ioctl(fd, ESP32_GDMA_MEMCPY, &req) < 0) {
		printf("FAIL: %s -- ioctl: %s\n", what, strerror(errno));
		failures++;
		return -1;
	}

	if (verbose)
		printf("info: %s -- the engine ran\n", what);

	return 0;
}

int main(int argc, char **argv)
{
	static uint8_t src[MAX_XFER];
	static uint8_t dst[MAX_XFER];
	static uint8_t want[MAX_XFER];
	int regs_fd = -1;
	int fd;
	int i;
	uint32_t date = 0;

	for (i = 1; i < argc; i++) {
		if (!strcmp(argv[i], "-v")) {
			verbose = 1;
		} else {
			printf("usage: dmatest2 [-v]\n");
			return 2;
		}
	}

	printf("dmatest2: the DMA engine, asked through the kernel\n");
	fflush(stdout);

	/* A. the device, and whether the engine is powered. */
	printf("\n--- A. the engine ---\n");

	fd = open(GDMA_DEV, O_RDWR);
	if (fd < 0) {
		printf("FAIL: open(%s): %s\n", GDMA_DEV, strerror(errno));
		printf("      (patch 0072 adds this device; is it in the image?)\n");
		return 1;
	}
	ok(1, "the DMA device opened");

	regs_fd = open(REGS_DEV, O_RDWR);
	if (regs_fd < 0) {
		printf("info: open(%s): %s -- skipping the date check\n",
		       REGS_DEV, strerror(errno));
	} else if (dev_read(regs_fd, GDMA_DATE, &date) == 0) {
		printf("info: GDMA_DATE reads 0x%08x, expected 0x%08x\n",
		       date, GDMA_DATE_VAL);
		if (date == GDMA_DATE_VAL) {
			ok(1, "the engine is powered and identifies itself");
		} else {
			ok(0, "the engine is powered and identifies itself");
			printf("      a zero here means the block is still in"
			       " reset or ungated, and every register write"
			       " below is accepted and ignored\n");
		}
	} else {
		printf("info: the register device refused a read of 0x%08lx\n",
		       (unsigned long)GDMA_DATE);
	}

	/*
	 * B. the smallest transfer the engine can do. If this fails, the
	 * problem is the engine, not the size.
	 *
	 * A failure here stops the run. The first version carried on to the
	 * next size, and on the board that second transfer took core 0 down
	 * with an IllegalInstruction and a watchdog reset -- core 0 is the
	 * ESP-IDF firmware, and a mis-addressed DMA write reaches it. One
	 * failed transfer is evidence; a second one is a risk to the board
	 * for no extra information.
	 */
	printf("\n--- B. 4 bytes ---\n");
	memset(dst, 0, sizeof(dst));
	memset(want, 0, sizeof(want));
	fill_pattern(src, 4, 0x11);
	fill_pattern(want, 4, 0x11);
	if (transfer(fd, src, dst, 4, "4 bytes") < 0) {
		printf("      stopping here: a failed transfer is not repeated\n");
		goto out;
	}
	compare(dst, want, 4, "4 bytes arrived intact");

	/* C. a small transfer, still one descriptor. */
	printf("\n--- C. 256 bytes ---\n");
	memset(dst, 0, sizeof(dst));
	memset(want, 0, sizeof(want));
	fill_pattern(src, 256, 0x22);
	fill_pattern(want, 256, 0x22);
	if (transfer(fd, src, dst, 256, "256 bytes") < 0)
		goto out;
	compare(dst, want, 256, "256 bytes arrived intact");

	/* D. one page -- the largest this version accepts. */
	printf("\n--- D. 4096 bytes ---\n");
	memset(dst, 0, sizeof(dst));
	memset(want, 0, sizeof(want));
	fill_pattern(src, MAX_XFER, 0x33);
	fill_pattern(want, MAX_XFER, 0x33);
	if (transfer(fd, src, dst, MAX_XFER, "4096 bytes") < 0)
		goto out;
	compare(dst, want, MAX_XFER, "4096 bytes arrived intact");

	/*
	 * E. a length the engine cannot express must be refused. The
	 * descriptor's size field counts words, so a length that is not a
	 * multiple of 4 has no correct encoding, and truncating it silently
	 * would be worse than refusing it.
	 */
	printf("\n--- E. a length that is not a multiple of 4 ---\n");
	memset(dst, 0, sizeof(dst));
	fill_pattern(src, 8, 0x44);
	errno = 0;
	if (transfer(fd, src, dst, 6, "6 bytes") == 0) {
		ok(0, "a 6-byte transfer is refused");
		printf("      the driver accepted it; the engine's size field"
		       " is in words and cannot express it\n");
	} else {
		ok(1, "a 6-byte transfer is refused");
	}

out:
	if (regs_fd >= 0)
		close(regs_fd);
	close(fd);

	printf("\n");
	if (failures == 0)
		printf("PASS: the engine moves bytes.\n");
	else
		printf("%d failure(s).\n", failures);

	return failures ? 1 : 0;
}
