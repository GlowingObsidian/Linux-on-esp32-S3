/*
 * mmprobe -- does a userspace mmap of a device window actually reach memory.
 *
 * Why this exists
 * ---------------
 * dmatest mmaps the esp32-regs SRAM window at 0x3fc88000 and a word written
 * through the mapping does not read back. Four runs, same result. Everything
 * around the mapping is known to be right:
 *
 *   - get_unmapped_area() returns 0x3fc88000 and mm/nommu.c makes that
 *     vma->vm_start, so the program's pointer is the physical address.
 *   - remap_pfn_range() on NOMMU checks addr == pfn << PAGE_SHIFT and returns
 *     -EINVAL otherwise. The mmap succeeded, so that identity held.
 *   - The cache is not in the path: CONFIG_MEMMAP_CACHEATTR=0xffff222f puts
 *     0x3fc88000 in region 1 at attribute 2, which arch/xtensa/Kconfig:634
 *     defines as cache bypass. XCHAL_DCACHE_SIZE is 0 as well.
 *   - ioremap() is the identity here (CONFIG_MMU is off, so
 *     include/asm-generic/io.h:1128 applies), so the kernel reading the same
 *     address through ioremap reads the same physical bytes the mapping
 *     should.
 *
 * None of that explains a store that does not land. This program is the
 * smallest thing that can tell the difference between the cases that remain:
 *
 *   A. Is the mapping even where we think? Print /proc/self/maps for the
 *      range, and print the pointer mmap returned.
 *   B. Does a write land, and does the read come back? Write a known word,
 *      read it back, and report both the value and whether it is the new
 *      value, the old value, or neither.
 *   C. Does the write land somewhere else? Scan the window for the value.
 *      If it turns up at a different address, the mapping is offset.
 *   D. Is a second write visible to the first? Write, read, write again,
 *      read again. A mapping that is a copy shows the first write and not
 *      the second.
 *   E. Does the kernel see the same bytes? Read the same word through
 *      /dev/esp32-regs, which goes through ioremap in the kernel. If the
 *      kernel sees the value the program wrote and the program does not,
 *      the write landed and the read path is the broken side.
 *
 * E needs the device to accept the SRAM range for single-word access. If it
 * refuses, that section says so and the rest still runs.
 *
 * Safety
 * ------
 * Writes only to the last 4 KB of the window, 0x3FCF0000-0x3FCFFFFF, and
 * restores the word it overwrites at the base of that page. Nothing else in
 * the window is touched.
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

#define REGS_DEV	"/dev/esp32-regs"

#define SRAM_BASE	0x3FC88000UL
#define SRAM_SIZE	0x78000UL
#define SRAM_END	(SRAM_BASE + SRAM_SIZE)

/* The last page of the window, so a mistake cannot land on core 0's data. */
#define WORK_BASE	(SRAM_BASE + SRAM_SIZE - PAGE_SIZE)

/*
 * The value written and looked for. Chosen to be recognisable in a hex dump
 * and unlikely to be there already.
 */
#define MARK		0xA5C3E10DUL
#define MARK2		0x5EEDF00DUL

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

static uint32_t rd(uint32_t phys)
{
	return *(volatile uint32_t *)(uintptr_t)phys;
}

static void wr(uint32_t phys, uint32_t val)
{
	*(volatile uint32_t *)(uintptr_t)phys = val;
}

/*
 * Read one word through the device, which goes through ioremap() in the
 * kernel. Returns 0 on success.
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
 * Print the lines of /proc/self/maps that cover the window. If the mapping is
 * not listed, or is listed at a different address or with different
 * permissions, that is the answer and nothing else in this program matters.
 */
static void show_maps(void)
{
	FILE *f;
	char line[512];
	unsigned long lo, hi;
	int found = 0;

	f = fopen("/proc/self/maps", "r");
	if (!f) {
		printf("info: /proc/self/maps: %s\n", strerror(errno));
		return;
	}

	printf("info: /proc/self/maps lines covering 0x%08lx-0x%08lx:\n",
	       (unsigned long)SRAM_BASE, (unsigned long)SRAM_END);

	while (fgets(line, sizeof(line), f)) {
		if (sscanf(line, "%lx-%lx", &lo, &hi) != 2)
			continue;
		if (hi <= SRAM_BASE || lo >= SRAM_END)
			continue;
		printf("      %s", line);
		found++;
	}
	fclose(f);

	if (!found) {
		printf("      (none)\n");
		ok(0, "the mapping appears in /proc/self/maps");
	} else {
		ok(1, "the mapping appears in /proc/self/maps");
	}
}

int main(int argc, char **argv)
{
	void *p;
	int regs_fd;
	uint32_t save;
	uint32_t v;
	int i;

	(void)argc;
	(void)argv;

	printf("mmprobe: userspace mmap of the esp32-regs SRAM window\n");
	printf("mmprobe: writes only inside 0x%08lx-0x%08lx\n",
	       (unsigned long)WORK_BASE,
	       (unsigned long)(WORK_BASE + PAGE_SIZE - 1));
	fflush(stdout);

	regs_fd = open(REGS_DEV, O_RDWR);
	if (regs_fd < 0) {
		printf("FAIL: open(%s): %s\n", REGS_DEV, strerror(errno));
		return 1;
	}

	/* A. the mapping. */
	printf("\n--- A. the mapping ---\n");

	p = mmap(NULL, SRAM_SIZE, PROT_READ | PROT_WRITE, MAP_SHARED,
		 regs_fd, 0);
	if (p == MAP_FAILED) {
		printf("FAIL: mmap: %s\n", strerror(errno));
		close(regs_fd);
		return 1;
	}

	printf("info: mmap returned %p; asked for physical 0x%08lx\n",
	       p, (unsigned long)SRAM_BASE);
	ok((unsigned long)p == SRAM_BASE,
	   "mmap returned the physical address, as NOMMU requires");

	show_maps();

	/* B. one write, one read, at the base of the work page. */
	printf("\n--- B. write then read ---\n");

	save = rd(WORK_BASE);
	printf("info: 0x%08lx held 0x%08x before the write\n",
	       (unsigned long)WORK_BASE, save);

	wr(WORK_BASE, (uint32_t)MARK);
	v = rd(WORK_BASE);
	printf("info: wrote 0x%08x, read 0x%08x\n", (uint32_t)MARK, v);

	if (v == (uint32_t)MARK) {
		ok(1, "the word written through the mapping reads back");
	} else if (v == save) {
		ok(0, "the word written through the mapping reads back");
		printf("      the read returned the OLD value: the store did not"
		       " land, or the read is served from elsewhere\n");
	} else {
		ok(0, "the word written through the mapping reads back");
		printf("      the read returned NEITHER the new nor the old value:"
		       " the address is not backed by the SRAM we think it is\n");
	}

	/* C. did the write land somewhere else in the window? */
	printf("\n--- C. where did the value go ---\n");
	{
		uint32_t found_at = 0;
		int nfound = 0;

		for (i = 0; i < (int)(SRAM_SIZE / 4); i++) {
			uint32_t a = (uint32_t)SRAM_BASE + (uint32_t)i * 4;

			if (a == (uint32_t)WORK_BASE)
				continue;
			if (rd(a) == (uint32_t)MARK) {
				if (!nfound)
					found_at = a;
				nfound++;
			}
		}

		if (nfound) {
			printf("info: 0x%08x also appears at 0x%08x (%d places)\n",
			       (uint32_t)MARK, found_at, nfound);
		} else {
			printf("info: 0x%08x appears nowhere else in the window\n",
			       (uint32_t)MARK);
		}
	}

	/* D. is the mapping live, or a copy taken at mmap time? */
	printf("\n--- D. second write ---\n");
	wr(WORK_BASE, (uint32_t)MARK2);
	v = rd(WORK_BASE);
	printf("info: wrote 0x%08x, read 0x%08x\n", (uint32_t)MARK2, v);
	if (v == (uint32_t)MARK2) {
		ok(1, "a second write is visible too");
	} else if (v == (uint32_t)MARK) {
		ok(0, "a second write is visible too");
		printf("      the read returned the FIRST write: the mapping is a"
		       " snapshot, not the window\n");
	} else {
		ok(0, "a second write is visible too");
	}

	/* E. does the kernel see what userspace wrote? */
	printf("\n--- E. the kernel's view ---\n");
	{
		uint32_t kview = 0;

		if (dev_read(regs_fd, (uint32_t)WORK_BASE, &kview) == 0) {
			printf("info: the device reads 0x%08lx as 0x%08x\n",
			       (unsigned long)WORK_BASE, kview);

			if (kview == (uint32_t)MARK2) {
				ok(1, "the kernel sees the value userspace wrote");
				printf("      the store landed; the userspace READ"
				       " path is the broken side\n");
			} else {
				ok(0, "the kernel sees the value userspace wrote");
				printf("      the kernel sees 0x%08x, userspace wrote"
				       " 0x%08x: the store never reached SRAM\n",
				       kview, (uint32_t)MARK2);
			}
		} else {
			printf("info: the device refused a read of 0x%08lx\n",
			       (unsigned long)WORK_BASE);
			printf("      (addr_allowed() covers the register windows"
			       " only; SRAM is mmap-only by design)\n");
		}
	}

	/* Restore what was there. */
	wr(WORK_BASE, save);

	printf("\n");
	if (failures == 0)
		printf("PASS: the mapping works.\n");
	else
		printf("%d failure(s).\n", failures);

	munmap(p, SRAM_SIZE);
	close(regs_fd);
	return failures ? 1 : 0;
}
