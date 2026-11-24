/* probe2 -- fbtest.c with the benchmark loops cut to one frame each.
 *
 * fbtest segfaults before main on this board; probe.c (same flags, same
 * startup, less code) runs clean. So the difference is in fbtest's own source,
 * and this file exists to find it by construction rather than by reading:
 * everything below is fbtest verbatim -- same includes, same getopt, same
 * usage(), same helpers, same order of calls -- with the two while loops
 * reduced to a single iteration and a STEP print before each call.
 *
 * The last STEP before the fault is the statement that faults.
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

#define RGB565(r, g, b) \
	((uint16_t)((((r) & 0x1f) << 11) | (((g) & 0x3f) << 5) | ((b) & 0x1f)))

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
		"usage: %s [-n frames] [-s seconds] [-b] [-q]\n"
		"  -n N   run N frames and report\n"
		"  -s S   run for S seconds (default 10)\n"
		"  -b     leave the console in graphics mode afterwards\n"
		"  -q     do not touch the console; measure only\n", argv0);
}

int main(int argc, char **argv)
{
	int frames_target = 0;
	(void)frames_target;
	int seconds = 10;
	int leave_graphics = 0;
	int quiet = 0;
	int opt;

	STEP("main entered");

	while ((opt = getopt(argc, argv, "n:s:bqh")) != -1) {
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
		default:
			usage(argv[0]);
			return 2;
		}
	}
	STEP("getopt done");

	int fb_fd = open("/dev/fb0", O_RDWR);
	if (fb_fd < 0) {
		fprintf(stderr, "probe2: open /dev/fb0: %s\n", strerror(errno));
		return 1;
	}
	STEP("fb0 opened");

	struct fb_var_screeninfo var;
	struct fb_fix_screeninfo fix;

	if (ioctl(fb_fd, FBIOGET_VSCREENINFO, &var) < 0) {
		fprintf(stderr, "probe2: FBIOGET_VSCREENINFO: %s\n", strerror(errno));
		close(fb_fd);
		return 1;
	}
	if (ioctl(fb_fd, FBIOGET_FSCREENINFO, &fix) < 0) {
		fprintf(stderr, "probe2: FBIOGET_FSCREENINFO: %s\n", strerror(errno));
		close(fb_fd);
		return 1;
	}
	STEP("ioctls done");

	size_t frame_bytes = (size_t)fix.line_length * (size_t)var.yres;
	int line_length_px = (int)(fix.line_length / (var.bits_per_pixel / 8));

	printf("probe2: %ux%u %ubpp, line_length %u, frame %u bytes\n",
	       var.xres, var.yres, var.bits_per_pixel, fix.line_length,
	       (unsigned)frame_bytes);
	fflush(stdout);

	uint16_t *fb = malloc(frame_bytes);
	if (!fb) {
		fprintf(stderr, "probe2: malloc %u: %s\n", (unsigned)frame_bytes,
			strerror(errno));
		close(fb_fd);
		return 1;
	}
	STEP("malloc done");

	int tty_fd = -1;
	int graphics_taken = 0;

	if (!quiet) {
		tty_fd = open("/dev/tty0", O_RDWR);
		if (tty_fd < 0)
			tty_fd = open("/dev/tty1", O_RDWR);
		if (tty_fd >= 0) {
			if (ioctl(tty_fd, KDSETMODE, KD_GRAPHICS) < 0) {
				fprintf(stderr, "probe2: KDSETMODE KD_GRAPHICS: %s\n",
					strerror(errno));
			} else {
				graphics_taken = 1;
				printf("probe2: console in graphics mode\n");
				fflush(stdout);
			}
		} else {
			fprintf(stderr, "probe2: no console tty: %s\n", strerror(errno));
		}
	}
	STEP("console setup done");

	int frame = 0;
	uint32_t start = now_ms();
	(void)start; (void)seconds;

	uint32_t t_flat_start = now_ms();
	int flat_frames = 0;

	/* One iteration of fbtest's flat loop. */
	STEP("before flat loop");
	draw_flat(fb, line_length_px, frame);
	STEP("after draw_flat");
	if (lseek(fb_fd, 0, SEEK_SET) < 0 ||
	    write(fb_fd, fb, frame_bytes) != (ssize_t)frame_bytes) {
		fprintf(stderr, "probe2: write: %s\n", strerror(errno));
	}
	STEP("after flat write");
	flat_frames++;
	frame++;

	uint32_t t_flat = now_ms() - t_flat_start;

	frame = 0;
	uint32_t t_bar_start = now_ms();
	int bar_frames = 0;

	/* One iteration of fbtest's bar loop. */
	STEP("before bar loop");
	draw_bar(fb, line_length_px, frame);
	STEP("after draw_bar");
	if (lseek(fb_fd, 0, SEEK_SET) < 0 ||
	    write(fb_fd, fb, frame_bytes) != (ssize_t)frame_bytes) {
		fprintf(stderr, "probe2: write: %s\n", strerror(errno));
	}
	STEP("after bar write");
	bar_frames++;
	frame++;

	uint32_t t_bar = now_ms() - t_bar_start;

	printf("probe2: flat fill  %d frames in %u ms = %.1f fps\n",
	       flat_frames, (unsigned)t_flat,
	       t_flat ? flat_frames * 1000.0 / t_flat : 0.0);
	printf("probe2: bar frame  %d frames in %u ms = %.1f fps\n",
	       bar_frames, (unsigned)t_bar,
	       t_bar ? bar_frames * 1000.0 / t_bar : 0.0);
	fflush(stdout);
	STEP("report printed");

	if (graphics_taken && !leave_graphics) {
		ioctl(tty_fd, KDSETMODE, KD_TEXT);
		printf("probe2: console restored to text mode\n");
	} else if (graphics_taken) {
		printf("probe2: console left in graphics mode (-b)\n");
	}
	fflush(stdout);
	STEP("console restored");

	free(fb);
	if (tty_fd >= 0)
		close(tty_fd);
	close(fb_fd);

	STEP("done");
	return 0;
}
