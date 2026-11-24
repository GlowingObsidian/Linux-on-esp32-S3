/*
 * fbdiag -- draw a frame whose every property is a clue, so that the three
 * unknowns of this 240x240 ST7789 module can be read off the glass.
 *
 * Why this exists. The controller is 240x320 and the module is 240x240, so 80
 * of the controller's rows are not wired to glass. Which 80 is a property of
 * the module's own wiring and cannot be read out of it -- it has to be tried.
 * Two more things are equally unknowable from here: whether the panel wants
 * MADCTL 0x00 (what fb_st7789v.c sends) or 0xC0 (what Adafruit sends for the
 * same module), and whether its colours come out inverted.
 *
 * fbtest answers "how fast". This answers "which of the three is wrong",
 * without a kernel rebuild between attempts: patch 0069 exposes row_offset,
 * col_offset and madctl as writable sysfs attributes, so the loop is
 *
 *   echo 80 > /sys/class/graphics/fb0/row_offset
 *
 * on the serial console with this program's picture on the panel, and the
 * picture moves. The point of the picture is that each wrong answer has a
 * different, recognisable appearance, so one look settles it.
 *
 * The frame, and what each part tells you:
 *
 *   - A 2px white border, inset by 2px, all the way round. Its only job is to
 *     show the frame's edges. If the border is missing from one side, the
 *     window is off the glass on that side; if a band of the *opposite* side's
 *     colour appears, the image is wrapped, which is the row offset.
 *
 *   - Four corner blocks, each a different colour, each labelled by its own
 *     position: top-left red, top-right green, bottom-left blue, bottom-right
 *     yellow. This is the orientation test. If the red block is not at the
 *     top-left, the MADCTL is wrong: red at top-right means the image is
 *     mirrored horizontally (MX), red at bottom-left means mirrored vertically
 *     (MY), red at bottom-right means both.
 *
 *   - A grey ramp across the middle, 32 steps from black to white. This is the
 *     inversion test. If it runs white to black instead, the panel is
 *     inverted -- fb_st7789v.c sends MIPI_DCS_ENTER_INVERT_MODE
 *     unconditionally (HSD20_IPS is 1) and that is the line to change.
 *
 *   - Three pure primaries under the ramp: red, green, blue, left to right.
 *     A second read on inversion, and the check that the byte order is right:
 *     if the red bar comes out blue, the panel is BGR and MADCTL bit 3 wants
 *     setting.
 *
 *   - A 16px checkerboard in the last strip. It is the only high-frequency
 *     pattern here, so it shows a one-pixel shift that a flat area hides, and
 *     it shows the SPI path is not dropping bytes.
 *
 *   - A row counter: a vertical bar one pixel wide, walking right by 16px a
 *     second, drawn on the top edge. It is the liveness check -- if it is
 *     moving, the deferred-IO path is pushing frames out, and if it is not,
 *     the picture below is the last frame that got through.
 *
 * Usage:  fbdiag [-s seconds] [-b] [-q] [-v]
 *   -s S   keep redrawing for S seconds (default: draw once and exit)
 *   -b     leave the console in graphics mode afterwards
 *   -q     do not touch the console at all -- draw only
 *   -v     print a line at each step
 *
 * 32-bit only, deliberately -- same reason as fbtest.c: this toolchain's
 * Xtensa LRA pass hits an internal compiler error (lra-constraints.cc:4234)
 * lowering any 64-bit value, so there is no int64_t below, no clock_gettime,
 * and no float. The ramp and the counter are integer work.
 *
 * Build (in the build container, with the project's own toolchain):
 *
 *   export XTENSA_GNU_CONFIG=/refs/esp32-linux-build/build/xtensa-dynconfig/esp32s3.so
 *   TC=/refs/esp32-linux-build/build/crosstool-NG/builds/xtensa-esp32s3-linux-uclibcfdpic/bin/xtensa-esp32s3-linux-uclibcfdpic
 *   $TC-gcc -Os -mfdpic -mauto-litpools -fPIC -ffunction-sections -fdata-sections \
 *       -std=gnu17 -Wall -Wextra -Werror \
 *       -Wl,--gc-sections,-z,stack-size=65536 fbdiag.c -o fbdiag
 *   $TC-strip --strip-unneeded -R .xt.prop -R .xt.lit fbdiag
 *
 * No mmap: fbtft_ops has no .fb_mmap, so mmap("/dev/fb0") returns ENODEV on
 * this driver. The framebuffer is reached with write() and lseek().
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

#define PANEL_W 240
#define PANEL_H 240

#define STEP(msg) do { printf("STEP %s\n", msg); fflush(stdout); } while (0)

static int verbose;

/* RGB565, the panel's native format. A macro rather than a function: Xtensa's
 * LRA pass is fragile on these loops and inlining the packing by hand keeps
 * the expression in one basic block. */
#define RGB565(r, g, b) \
	((uint16_t)((((r) & 0x1f) << 11) | (((g) & 0x3f) << 5) | ((b) & 0x1f)))

#define BLACK   RGB565(0, 0, 0)
#define WHITE   RGB565(31, 63, 31)
#define RED     RGB565(31, 0, 0)
#define GREEN   RGB565(0, 63, 0)
#define BLUE    RGB565(0, 0, 31)
#define YELLOW  RGB565(31, 63, 0)
#define GREY(v) RGB565((v) / 8, (v) / 4, (v) / 8)

/* Milliseconds since boot, from the kernel's own monotonic clock.
 *
 * clock_gettime(CLOCK_MONOTONIC) is the obvious call and its struct timespec
 * is 64-bit, which this toolchain cannot lower (see the header). /proc/uptime
 * carries the same number as text and costs one small read.
 */
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

	if (fd < 0)
		return 0;
	n = read(fd, buf, sizeof(buf) - 1);
	close(fd);
	if (n <= 0)
		return 0;
	buf[n] = '\0';

	/* "12345.67 8901.23" -- seconds with two decimals. */
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
	return sec * 1000 + frac;
}

/* One horizontal run of one colour, clipped to the panel. */
static void hline(uint16_t *fb, int stride, int y, int x0, int x1, uint16_t c)
{
	int x;

	if (y < 0 || y >= PANEL_H)
		return;
	if (x0 < 0)
		x0 = 0;
	if (x1 > PANEL_W)
		x1 = PANEL_W;
	for (x = x0; x < x1; x++)
		fb[(size_t)y * (size_t)stride + (size_t)x] = c;
}

/* One vertical run of one colour, clipped to the panel. */
static void vline(uint16_t *fb, int stride, int x, int y0, int y1, uint16_t c)
{
	int y;

	if (x < 0 || x >= PANEL_W)
		return;
	if (y0 < 0)
		y0 = 0;
	if (y1 > PANEL_H)
		y1 = PANEL_H;
	for (y = y0; y < y1; y++)
		fb[(size_t)y * (size_t)stride + (size_t)x] = c;
}

/* A filled rectangle. The corner blocks and the primaries are all this. */
static void fill_rect(uint16_t *fb, int stride, int x0, int y0, int w, int h,
		      uint16_t c)
{
	int y;

	for (y = y0; y < y0 + h; y++)
		hline(fb, stride, y, x0, x0 + w, c);
}

/*
 * The whole diagnostic frame.
 *
 * `tick` only moves the counter bar; everything else is static, so a frame
 * that arrives with the counter in a new place and the rest unchanged is
 * exactly what a healthy deferred-IO path looks like.
 */
static void draw_diag(uint16_t *fb, int stride, int tick)
{
	int i;

	/* Start from black, so anything not drawn is unambiguous. */
	fill_rect(fb, stride, 0, 0, PANEL_W, PANEL_H, BLACK);

	/* The border, inset 2px so it cannot be confused with the panel's own
	 * edge, and 2px thick so a one-pixel shift leaves it visible. */
	fill_rect(fb, stride, 2, 2, PANEL_W - 4, 2, WHITE);
	fill_rect(fb, stride, 2, PANEL_H - 4, PANEL_W - 4, 2, WHITE);
	fill_rect(fb, stride, 2, 2, 2, PANEL_H - 4, WHITE);
	fill_rect(fb, stride, PANEL_W - 4, 2, 2, PANEL_H - 4, WHITE);

	/* The four corner blocks. 40x40, inset 8px so each sits clear of the
	 * border and of the ramp. Their colours are the orientation test:
	 * red belongs at the top-left and nowhere else. */
	fill_rect(fb, stride, 8, 8, 40, 40, RED);                    /* TL */
	fill_rect(fb, stride, PANEL_W - 48, 8, 40, 40, GREEN);       /* TR */
	fill_rect(fb, stride, 8, PANEL_H - 48, 40, 40, BLUE);        /* BL */
	fill_rect(fb, stride, PANEL_W - 48, PANEL_H - 48, 40, 40,
		  YELLOW);                                           /* BR */

	/* The grey ramp: 32 steps of 8px, black to white, left to right.
	 * Inverted, it runs white to black. */
	for (i = 0; i < 32; i++)
		fill_rect(fb, stride, 8 + i * 7, 100, 7, 24, GREY(i * 8));

	/* The primaries, left to right. Red on the left is the second
	 * inversion read and the BGR check: red arriving as blue means MADCTL
	 * bit 3 wants setting. */
	fill_rect(fb, stride, 8, 132, 72, 24, RED);
	fill_rect(fb, stride, 84, 132, 72, 24, GREEN);
	fill_rect(fb, stride, 160, 132, 72, 24, BLUE);

	/* A 16px checkerboard over the last strip. The only high-frequency
	 * pattern in the frame, so it is what shows a one-pixel shift. */
	for (i = 0; i < PANEL_W - 16; i += 16)
		fill_rect(fb, stride, 8 + i, 168, 8, 16, WHITE);
	for (i = 0; i < PANEL_W - 16; i += 16)
		fill_rect(fb, stride, 16 + i, 184, 8, 16, WHITE);

	/* The counter bar, on the top edge inside the border. It walks right
	 * by 16px a second and wraps. If it is not moving, the picture is
	 * stale and the SPI path is the problem, not the geometry. */
	vline(fb, stride, 4 + (tick * 16) % (PANEL_W - 8), 6, 12, WHITE);
}

static void usage(const char *argv0)
{
	fprintf(stderr,
		"usage: %s [-s seconds] [-b] [-q] [-v]\n"
		"  -s S   redraw for S seconds (default: draw once and exit)\n"
		"  -b     leave the console in graphics mode afterwards\n"
		"  -q     do not touch the console; draw only\n"
		"  -v     print a line at each step\n", argv0);
}

int main(int argc, char **argv)
{
	int seconds = 0;
	int leave_graphics = 0;
	int quiet = 0;
	int opt;
	int fb_fd;
	struct fb_var_screeninfo var;
	struct fb_fix_screeninfo fix;
	size_t frame_bytes;
	int stride;
	uint16_t *fb;
	int tty_fd = -1;
	int graphics_taken = 0;

	while ((opt = getopt(argc, argv, "s:bqvh")) != -1) {
		switch (opt) {
		case 's':
			seconds = atoi(optarg);
			break;
		case 'b':
			leave_graphics = 1;
			break;
		case 'q':
			quiet = 1;
			break;
		case 'v':
			verbose = 1;
			break;
		default:
			usage(argv[0]);
			return 2;
		}
	}

	if (verbose)
		STEP("main entered");

	fb_fd = open("/dev/fb0", O_RDWR);
	if (fb_fd < 0) {
		fprintf(stderr, "fbdiag: open /dev/fb0: %s\n", strerror(errno));
		return 1;
	}
	if (verbose)
		STEP("fb0 opened");

	if (ioctl(fb_fd, FBIOGET_VSCREENINFO, &var) < 0) {
		fprintf(stderr, "fbdiag: FBIOGET_VSCREENINFO: %s\n",
			strerror(errno));
		close(fb_fd);
		return 1;
	}
	if (ioctl(fb_fd, FBIOGET_FSCREENINFO, &fix) < 0) {
		fprintf(stderr, "fbdiag: FBIOGET_FSCREENINFO: %s\n",
			strerror(errno));
		close(fb_fd);
		return 1;
	}
	if (verbose)
		STEP("ioctls done");

	frame_bytes = (size_t)fix.line_length * (size_t)var.yres;
	stride = (int)(fix.line_length / (var.bits_per_pixel / 8));

	printf("fbdiag: %ux%u %ubpp, line_length %u, stride %d, frame %u bytes\n",
	       var.xres, var.yres, var.bits_per_pixel, fix.line_length, stride,
	       (unsigned)frame_bytes);
	printf("fbdiag: rotate %u, red offset %u green %u blue %u\n",
	       var.rotate, var.red.offset, var.green.offset, var.blue.offset);

	/* A frame buffer is allocated once and reused, so the draw loop does no
	 * allocation at all. */
	fb = malloc(frame_bytes);
	if (!fb) {
		fprintf(stderr, "fbdiag: malloc %u: %s\n", (unsigned)frame_bytes,
			strerror(errno));
		close(fb_fd);
		return 1;
	}

	/* Take the console out of text mode so fbcon stops repainting over the
	 * picture. /dev/tty0 is the active console; KD_GRAPHICS is the standard
	 * "a program owns the framebuffer now" signal. */
	if (!quiet) {
		tty_fd = open("/dev/tty0", O_RDWR);
		if (tty_fd < 0)
			tty_fd = open("/dev/tty1", O_RDWR);
		if (tty_fd >= 0) {
			if (ioctl(tty_fd, KDSETMODE, KD_GRAPHICS) < 0) {
				fprintf(stderr,
					"fbdiag: KDSETMODE KD_GRAPHICS: %s\n",
					strerror(errno));
			} else {
				graphics_taken = 1;
				printf("fbdiag: console in graphics mode\n");
			}
		} else {
			fprintf(stderr, "fbdiag: no console tty: %s\n",
				strerror(errno));
		}
	}
	if (verbose)
		STEP("console setup done");

	/* One draw, then hold. With -s the counter keeps moving; without it the
	 * frame is drawn once and left on the glass, which is the state worth
	 * staring at while the sysfs knobs are tried. */
	{
		int tick = 0;
		uint32_t deadline = 0;

		if (seconds > 0)
			deadline = now_ms() + (uint32_t)seconds * 1000;

		do {
			if (verbose)
				STEP("draw_diag");
			draw_diag(fb, stride, tick);
			if (lseek(fb_fd, 0, SEEK_SET) < 0 ||
			    write(fb_fd, fb, frame_bytes) != (ssize_t)frame_bytes) {
				fprintf(stderr, "fbdiag: write: %s\n",
					strerror(errno));
				break;
			}
			tick++;
			if (seconds > 0 && now_ms() < deadline) {
				/* ~4 fps: fast enough that the counter
				 * visibly moves, slow enough that the SPI path
				 * is never the reason a frame is late. */
				usleep(250000);
				continue;
			}
			break;
		} while (1);
	}

	if (graphics_taken && !leave_graphics) {
		ioctl(tty_fd, KDSETMODE, KD_TEXT);
		printf("fbdiag: console restored to text mode\n");
	} else if (graphics_taken) {
		printf("fbdiag: console left in graphics mode (-b)\n");
	}

	free(fb);
	if (tty_fd >= 0)
		close(tty_fd);
	close(fb_fd);

	if (verbose)
		STEP("done");
	return 0;
}
