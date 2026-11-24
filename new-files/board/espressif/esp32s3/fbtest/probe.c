/* Step-by-step probe: find where fbtest dies on the board.
 *
 * The first version of this proved the whole fb path works -- open, both
 * ioctls, malloc(40960), lseek, and a 40960-byte write all succeed on the
 * board. So the segfault in fbtest is in code that version did not have:
 * the /proc/uptime clock, the two draw loops, or KDSETMODE. This version
 * covers those, in the same order fbtest reaches them.
 *
 * Prints and flushes at each step, so the last line before a crash is the
 * step that faulted. Nothing here is clever; it exists to be read.
 */
#include <errno.h>
#include <fcntl.h>
#include <linux/fb.h>
#include <linux/kd.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <unistd.h>

#define STEP(msg) do { printf("STEP %s\n", msg); fflush(stdout); } while (0)

#define PANEL_W 128
#define PANEL_H 160

#define RGB565(r, g, b) \
	((uint16_t)((((r) & 0x1f) << 11) | (((g) & 0x3f) << 5) | ((b) & 0x1f)))

/* Copied verbatim from fbtest.c, so this tests the real thing. */
static uint32_t now_ms(void)
{
	char buf[32];
	int fd = open("/proc/uptime", O_RDONLY);
	ssize_t n;
	uint32_t sec = 0;
	uint32_t frac = 0;
	int seen_dot = 0;
	uint32_t scale = 100;
	char *p;

	STEP("now_ms: opened /proc/uptime");
	if (fd < 0) {
		printf("STEP now_ms: open failed errno=%d (%s)\n", errno, strerror(errno));
		fflush(stdout);
		return 0;
	}
	n = read(fd, buf, sizeof(buf) - 1);
	close(fd);
	printf("STEP now_ms: read returned %d\n", (int)n);
	fflush(stdout);
	if (n <= 0)
		return 0;
	buf[n] = '\0';
	printf("STEP now_ms: buf='%s'\n", buf);
	fflush(stdout);

	for (p = buf; *p && *p != ' '; p++) {
		if (*p == '.') {
			seen_dot = 1;
			continue;
		}
		if (*p < '0' || *p > '9')
			break;
		if (!seen_dot) {
			sec = sec * 10 + (uint32_t)(*p - '0');
		} else {
			frac += (uint32_t)(*p - '0') * scale;
			scale /= 10;
		}
	}
	printf("STEP now_ms: parsed sec=%u frac=%u\n", (unsigned)sec, (unsigned)frac);
	fflush(stdout);
	return sec * 1000 + frac;
}

static void draw_flat(uint16_t *fb, int line_length_px, int frame)
{
	uint16_t colour = RGB565(frame, frame * 2, 31);
	size_t i;
	size_t pixels = (size_t)PANEL_H * (size_t)line_length_px;

	for (i = 0; i < pixels; i++)
		fb[i] = colour;
}

static void draw_bar(uint16_t *fb, int line_length_px, int frame)
{
	int x0 = (frame * 4) % PANEL_W;
	int y, x;

	for (y = 0; y < PANEL_H; y++) {
		uint16_t *row = fb + (size_t)y * (size_t)line_length_px;

		for (x = 0; x < PANEL_W; x++) {
			if (x >= x0 && x < x0 + 16)
				row[x] = RGB565(31, 63, 31);
			else
				row[x] = RGB565(x + frame, y + frame, x ^ y);
		}
	}
}

int main(void)
{
	struct fb_var_screeninfo var;
	struct fb_fix_screeninfo fix;
	int fd;
	size_t frame_bytes;
	uint16_t *fb;
	int line_length_px;

	STEP("start");

	fd = open("/dev/fb0", O_RDWR);
	printf("STEP open fd=%d errno=%d (%s)\n", fd, errno, strerror(errno));
	fflush(stdout);
	if (fd < 0)
		return 1;

	if (ioctl(fd, FBIOGET_VSCREENINFO, &var) < 0) {
		printf("STEP vinfo failed errno=%d (%s)\n", errno, strerror(errno));
		fflush(stdout);
		return 1;
	}
	if (ioctl(fd, FBIOGET_FSCREENINFO, &fix) < 0) {
		printf("STEP finfo failed errno=%d (%s)\n", errno, strerror(errno));
		fflush(stdout);
		return 1;
	}
	frame_bytes = (size_t)fix.line_length * (size_t)var.yres;
	line_length_px = (int)(fix.line_length / (var.bits_per_pixel / 8));
	printf("STEP geometry: frame_bytes=%u line_length_px=%d\n",
	       (unsigned)frame_bytes, line_length_px);
	fflush(stdout);

	STEP("before malloc");
	fb = malloc(frame_bytes);
	printf("STEP malloc returned %p\n", (void *)fb);
	fflush(stdout);
	if (!fb)
		return 1;

	/* The clock, exactly as fbtest calls it. */
	STEP("before now_ms #1");
	uint32_t t1 = now_ms();
	printf("STEP now_ms #1 = %u\n", (unsigned)t1);
	fflush(stdout);

	/* The flat loop: one frame. */
	STEP("before draw_flat");
	draw_flat(fb, line_length_px, 0);
	STEP("after draw_flat");

	STEP("before write (flat)");
	ssize_t n = write(fd, fb, frame_bytes);
	printf("STEP write returned %d\n", (int)n);
	fflush(stdout);

	/* The bar loop: one frame. */
	STEP("before draw_bar");
	draw_bar(fb, line_length_px, 0);
	STEP("after draw_bar");

	STEP("before lseek+write (bar)");
	lseek(fd, 0, SEEK_SET);
	n = write(fd, fb, frame_bytes);
	printf("STEP write returned %d\n", (int)n);
	fflush(stdout);

	/* The console ioctl, which is what -q skips. */
	STEP("before open /dev/tty0");
	int tty = open("/dev/tty0", O_RDWR);
	printf("STEP tty fd=%d errno=%d (%s)\n", tty, errno, strerror(errno));
	fflush(stdout);
	if (tty >= 0) {
		STEP("before KDSETMODE KD_GRAPHICS");
		if (ioctl(tty, KDSETMODE, KD_GRAPHICS) < 0) {
			printf("STEP KDSETMODE failed errno=%d (%s)\n",
			       errno, strerror(errno));
			fflush(stdout);
		} else {
			STEP("KDSETMODE KD_GRAPHICS ok");
			STEP("before KDSETMODE KD_TEXT");
			ioctl(tty, KDSETMODE, KD_TEXT);
			STEP("KDSETMODE KD_TEXT ok");
		}
		close(tty);
	}

	STEP("before second now_ms");
	uint32_t t2 = now_ms();
	printf("STEP now_ms #2 = %u\n", (unsigned)t2);
	fflush(stdout);

	STEP("done");
	close(fd);
	return 0;
}
