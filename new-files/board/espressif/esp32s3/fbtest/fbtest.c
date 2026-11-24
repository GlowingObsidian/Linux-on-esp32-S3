/*
 * fbtest -- measure how fast this board can put frames on the ST7789 panel,
 * from a compiled program rather than from micropython.
 *
 * Why this exists. The panel was measured from micropython and came out at
 * 1.4-100 fps for the same frame, varying only with how much Python ran per
 * frame. That is the interpreter, not the display: a Python `for` over the
 * rows costs ~0.15 ms per iteration on this 240 MHz Xtensa, which is tens of
 * milliseconds of pure interpretation per frame before the kernel sees
 * anything. A compiled program removes that variable entirely, so this is the
 * number that says what the panel and the SPI path can actually do.
 *
 * Those figures were taken on the 128x160 ST7735R this image shipped first.
 * The panel is now a 240x240 ST7789, so the frame is 115200 bytes rather than
 * 40960 and every rate below scales with it.
 *
 * It is also the first working piece of "draw on the panel from a program".
 * fbcon owns the framebuffer whenever the console is bound to it, and it
 * repaints over anything written underneath it. KD_GRAPHICS is the standard
 * way to tell fbcon to stop drawing and hand the surface over; this program
 * takes it, draws, and gives it back, so the console comes back exactly as it
 * was when the program exits.
 *
 * 32-bit only, deliberately. This toolchain's Xtensa LRA pass hits an internal
 * compiler error (lra-constraints.cc:4234) lowering any 64-bit value -- a bare
 * `int64_t f(void) { return 1; }` is enough to reproduce it. So there is no
 * int64_t anywhere below, no clock_gettime (its struct timespec is 64-bit),
 * and the millisecond counter wraps every 49.7 days, which is longer than any
 * run of this program.
 *
 * Build (in the build container, with the project's own toolchain):
 *
 *   xtensa-esp32s3-linux-uclibcfdpic-gcc -Os -mfdpic -mauto-litpools -fPIC \
 *       -ffunction-sections -fdata-sections -std=gnu17 -Wall -Wextra -Werror \
 *       -Wl,--gc-sections,-z,stack-size=65536 fbtest.c -o fbtest
 *
 * The flags are the ones build/container-build.sh uses for espctl, so this
 * links against the same uclibc-fdpic and runs on the board as-is.
 *
 * Usage:  fbtest [-n frames] [-s seconds] [-b] [-q]
 *   -n N   run N frames and report
 *   -s S   run for S seconds (default 10)
 *   -b     leave the console in graphics mode afterwards (for a paint app)
 *   -q     do not touch the console at all -- measure only
 *   -v     print a line at each step inside the loops (diagnosis)
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
#include <sys/mman.h>
#include <unistd.h>

/* The panel is 240x240 RGB565. The frame size is read from the driver rather
 * than assumed, so this keeps working if the panel changes. */
#define PANEL_W 240
#define PANEL_H 240

/* Prints and flushes one line, so the last line before a fault names the
 * statement that faulted. Off unless -v is given: in the loops it would cost
 * more than the work being measured. */
#define STEP(msg) do { printf("STEP %s\n", msg); fflush(stdout); } while (0)

static int verbose;

/* Milliseconds since boot, from the kernel's own monotonic clock.
 *
 * clock_gettime(CLOCK_MONOTONIC) is the obvious call and it is what this used
 * first, but its struct timespec is 64-bit and this toolchain cannot lower
 * that (see the header). /proc/uptime carries the same number as text and
 * costs one small read, which is nothing against a frame.
 *
 * The read is done once per call and the parse is plain 32-bit integer work.
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

/* RGB565, the panel's native format. A macro rather than a function: Xtensa's
 * LRA pass is fragile on this loop and inlining the packing by hand keeps the
 * expression in one basic block. */
#define RGB565(r, g, b) \
	((uint16_t)((((r) & 0x1f) << 11) | (((g) & 0x3f) << 5) | ((b) & 0x1f)))

/* One frame of a sweeping bar, written straight into the mapped framebuffer.
 *
 * This is the whole point of the program: no syscall per row, no interpreter,
 * just stores into memory the kernel has already mapped. The panel is driven
 * by fbtft's deferred IO, which notices the writes and pushes the damaged
 * lines out over SPI on its own schedule. */
static void draw_bar(uint16_t *fb, int line_length_px, int frame)
{
	int x0 = (frame * 4) % PANEL_W;
	int y, x;

	for (y = 0; y < PANEL_H; y++) {
		uint16_t *row = fb + (size_t)y * (size_t)line_length_px;

		for (x = 0; x < PANEL_W; x++) {
			/* A moving vertical bar over a gradient, so the frame is
			 * visibly different each time and the damage map is
			 * genuinely dirty -- a frame that does not change would
			 * measure the skip path, not the draw path. */
			if (x >= x0 && x < x0 + 16)
				row[x] = RGB565(31, 63, 31);
			else
				row[x] = RGB565(x + frame, y + frame, x ^ y);
		}
	}
}

/* The same frame as a flat fill: every pixel identical. This is the cheapest
 * possible frame and gives the upper bound on what the panel can do when the
 * CPU work is trivial. */
static void draw_flat(uint16_t *fb, int line_length_px, int frame)
{
	uint16_t colour = RGB565(frame, frame * 2, 31);
	size_t i;
	size_t pixels = (size_t)PANEL_H * (size_t)line_length_px;

	for (i = 0; i < pixels; i++)
		fb[i] = colour;
}

static void usage(const char *argv0)
{
	fprintf(stderr,
		"usage: %s [-n frames] [-s seconds] [-b] [-q] [-v]\n"
		"  -n N   run N frames and report\n"
		"  -s S   run for S seconds (default 10)\n"
		"  -b     leave the console in graphics mode afterwards\n"
		"  -q     do not touch the console; measure only\n"
		"  -v     print a line at each step inside the loops\n", argv0);
}

int main(int argc, char **argv)
{
	int frames_target = 0;
	int seconds = 10;
	int leave_graphics = 0;
	int quiet = 0;
	int opt;

	while ((opt = getopt(argc, argv, "n:s:bqvh")) != -1) {
		switch (opt) {
		case 'n':
			frames_target = atoi(optarg);
			break;
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

	int fb_fd = open("/dev/fb0", O_RDWR);
	if (fb_fd < 0) {
		fprintf(stderr, "fbtest: open /dev/fb0: %s\n", strerror(errno));
		return 1;
	}
	if (verbose)
		STEP("fb0 opened");

	struct fb_var_screeninfo var;
	struct fb_fix_screeninfo fix;

	if (ioctl(fb_fd, FBIOGET_VSCREENINFO, &var) < 0) {
		fprintf(stderr, "fbtest: FBIOGET_VSCREENINFO: %s\n", strerror(errno));
		close(fb_fd);
		return 1;
	}
	if (ioctl(fb_fd, FBIOGET_FSCREENINFO, &fix) < 0) {
		fprintf(stderr, "fbtest: FBIOGET_FSCREENINFO: %s\n", strerror(errno));
		close(fb_fd);
		return 1;
	}
	if (verbose)
		STEP("ioctls done");

	size_t frame_bytes = (size_t)fix.line_length * (size_t)var.yres;
	int line_length_px = (int)(fix.line_length / (var.bits_per_pixel / 8));

	printf("fbtest: %ux%u %ubpp, line_length %u, frame %u bytes\n",
	       var.xres, var.yres, var.bits_per_pixel, fix.line_length,
	       (unsigned)frame_bytes);

	/* No mmap. fbtft_ops (fbtft-core.c:428) has no .fb_mmap -- it is
	 * FB_DEFAULT_DEFERRED_OPS plus setcolreg and blank, and nothing else --
	 * so mmap("/dev/fb0") returns ENODEV on this driver and always will.
	 * The framebuffer is reached with write() and lseek(), which is what
	 * the deferred-IO path is built for: fb_write copies into
	 * screen_buffer and marks the damaged lines, and fbtft pushes them out
	 * over SPI on its own schedule.
	 *
	 * A frame buffer is allocated once and reused, so the draw loops do no
	 * allocation at all. */
	uint16_t *fb = malloc(frame_bytes);
	if (!fb) {
		fprintf(stderr, "fbtest: malloc %u: %s\n", (unsigned)frame_bytes,
			strerror(errno));
		close(fb_fd);
		return 1;
	}

	/* Take the console out of text mode so fbcon stops repainting over
	 * us. /dev/tty0 is the active console; KD_GRAPHICS is the standard
	 * "a program owns the framebuffer now" signal. Without this the panel
	 * shows the console and nothing else, which is what the micropython
	 * runs hit. */
	int tty_fd = -1;
	int graphics_taken = 0;

	if (!quiet) {
		tty_fd = open("/dev/tty0", O_RDWR);
		if (tty_fd < 0)
			tty_fd = open("/dev/tty1", O_RDWR);
		if (tty_fd >= 0) {
			if (ioctl(tty_fd, KDSETMODE, KD_GRAPHICS) < 0) {
				fprintf(stderr, "fbtest: KDSETMODE KD_GRAPHICS: %s\n",
					strerror(errno));
			} else {
				graphics_taken = 1;
				printf("fbtest: console in graphics mode\n");
			}
		} else {
			fprintf(stderr, "fbtest: no console tty: %s\n", strerror(errno));
		}
	}
	if (verbose)
		STEP("console setup done");

	int frame = 0;
	uint32_t start = now_ms();
	uint32_t deadline = start + (uint32_t)seconds * 1000;

	/* Two passes, so the report says which one is the limit. The flat
	 * fill is the floor of CPU work; the bar is a realistic frame. */
	uint32_t t_flat_start = now_ms();
	int flat_frames = 0;

	while (now_ms() < deadline) {
		if (verbose)
			STEP("flat: loop head");
		draw_flat(fb, line_length_px, frame);
		if (verbose)
			STEP("flat: draw_flat done");
		if (lseek(fb_fd, 0, SEEK_SET) < 0 ||
		    write(fb_fd, fb, frame_bytes) != (ssize_t)frame_bytes) {
			fprintf(stderr, "fbtest: write: %s\n", strerror(errno));
			break;
		}
		if (verbose)
			STEP("flat: write done");
		flat_frames++;
		frame++;
		if (frames_target && frame >= frames_target)
			break;
	}
	uint32_t t_flat = now_ms() - t_flat_start;

	frame = 0;
	uint32_t t_bar_start = now_ms();
	int bar_frames = 0;

	while (now_ms() < deadline + 1000) {
		if (verbose)
			STEP("bar: loop head");
		draw_bar(fb, line_length_px, frame);
		if (verbose)
			STEP("bar: draw_bar done");
		if (lseek(fb_fd, 0, SEEK_SET) < 0 ||
		    write(fb_fd, fb, frame_bytes) != (ssize_t)frame_bytes) {
			fprintf(stderr, "fbtest: write: %s\n", strerror(errno));
			break;
		}
		if (verbose)
			STEP("bar: write done");
		bar_frames++;
		frame++;
		if (frames_target && frame >= frames_target)
			break;
	}
	uint32_t t_bar = now_ms() - t_bar_start;

	printf("fbtest: flat fill  %d frames in %u ms = %.1f fps\n",
	       flat_frames, (unsigned)t_flat,
	       t_flat ? flat_frames * 1000.0 / t_flat : 0.0);
	printf("fbtest: bar frame  %d frames in %u ms = %.1f fps\n",
	       bar_frames, (unsigned)t_bar,
	       t_bar ? bar_frames * 1000.0 / t_bar : 0.0);

	/* Hand the console back. KD_TEXT restores fbcon, which repaints the
	 * whole screen from its own buffer -- so the console comes back
	 * exactly as it was, not blanked. */
	if (graphics_taken && !leave_graphics) {
		ioctl(tty_fd, KDSETMODE, KD_TEXT);
		printf("fbtest: console restored to text mode\n");
	} else if (graphics_taken) {
		printf("fbtest: console left in graphics mode (-b)\n");
	}

	free(fb);
	if (tty_fd >= 0)
		close(tty_fd);
	close(fb_fd);

	if (verbose)
		STEP("done");
	return 0;
}
