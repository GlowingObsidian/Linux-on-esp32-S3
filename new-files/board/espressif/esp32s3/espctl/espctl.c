/* SPDX-License-Identifier: MIT */
/*
 * espctl -- GPIO, I2C and system readout for the Arduino Nano ESP32.
 *
 * The board's GPIOs are addressed by Arduino's own names (D0-D13, A0-A7) plus
 * the three channels of the RGB LED, and those names are the interface here.
 * A bare GPIO number still works.
 *
 * D0 and D1 are the exception, and are deliberately not offered. They are
 * UART0's RX and TX (GPIO44 and GPIO43), and this kernel's command line carries
 * `console=ttyS0`, so the kernel is already writing to D1 and reading D0. They
 * are not free pins with something attached; they are a live console, and
 * driving them would corrupt it rather than do anything useful. Named here
 * rather than silently missing, because "my pin is not in the list" and "this
 * pin cannot be used" are different answers.
 *
 * The pin numbers below cannot be measured on the board. Unlike the LED
 * pins -- whose wiring was confirmed by driving them -- nothing exposes the
 * silkscreen mapping: there is no board ID EEPROM on a Nano ESP32, and the
 * device tree has no node that carries it (checked on the running board: the
 * top level holds only #address-cells, #size-cells, aliases, chosen, clocks,
 * compatible, cpus, i2c0, interrupt-parent, memory@0, name, pic and soc).
 * So the table is stated once and cites its source; everything else here --
 * which chip, how many lines, the clock -- is discovered.
 *
 * The numbers come from Arduino's variant for this board
 * (variants/arduino_nano_nora/pins_arduino.h in arduino-esp32) and match
 * CircuitPython's ports/espressif/boards/arduino_nano_esp32/pins.c. They are
 * consistent with the S3's own fixed assignments: 19/20 are USB D-/D+, 26 is
 * the PSRAM chip select, 27-37 belong to the flash and the octal PSRAM, and
 * 39-42 are JTAG. None of those are on the header, and none are listed below.
 *
 * Worth knowing when reading the refusals: the kernel does not enforce any of
 * that. Every one of those lines exports and claims cleanly through sysfs on
 * this image -- 19, 26, 39 and 45 all did, measured -- so the exclusions here
 * are the only thing standing between a typo and a dead PSRAM. The table is a
 * policy, and the policy lives in this file.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <dirent.h>
#include <limits.h>
#include <fcntl.h>
#include <errno.h>
#include <ctype.h>
#include <sys/ioctl.h>
#include <sys/utsname.h>
#include <sys/sysinfo.h>
#include <linux/gpio.h>
#include <linux/i2c-dev.h>

/* The kernel registers three gpiochips on this board and the S3's is not the
 * first: the two 600c0018.clk_gpio / 600c001c.clk_gpio banks come before it,
 * and the /dev/gpiochipN number follows registration order rather than any
 * stable scheme. So the chip is found by label and never by number. The
 * candidates come from listing /dev rather than probing a fixed range -- the
 * number is not bounded by anything this program can assume, and a range that
 * stopped short would report "no gpiochip at all" for a board that has one.
 * ESPCTL_GPIOCHIP overrides the search with an explicit path.
 *
 * Offsets passed to the line ioctl are relative to the chip, and this chip's
 * lines start at its own GPIO 0, so the offset is the pin number as written.
 * That stays true if a kernel change ever moves the chip's global base. */
#define GPIO_LABEL "esp32s3-gpio"
#define GPIO_MAX_CHIPS 64

/* The Nano's header, as the silkscreen names it. `note` carries what the board
 * cannot tell you from the pin number: what the header prints beside it, and
 * what the line is doing before anything drives it. It is left out for the pins
 * that are just a GPIO. Written with designated initializers because the note
 * is the exception rather than the rule, and because a table of positional
 * three-field rows is one insertion away from silently shifting every note onto
 * the wrong pin. */
static const struct pin_name {
	const char *name;
	int gpio;
	const char *note;
} builtin_pins[] = {
	{ .name = "D2", .gpio = 5 },
	{ .name = "D3", .gpio = 6 },
	{ .name = "D4", .gpio = 7 },
	{ .name = "D5", .gpio = 8 },
	{ .name = "D6", .gpio = 9 },
	{ .name = "D7", .gpio = 10 },
	{ .name = "D8", .gpio = 17 },
	{ .name = "D9", .gpio = 18 },
	{ .name = "D10", .gpio = 21, .note = "chip select in the default SPI assignment" },
	{ .name = "D11", .gpio = 38, .note = "COPI (MOSI)" },
	{ .name = "D12", .gpio = 47, .note = "CIPO (MISO)" },
	{ .name = "D13", .gpio = 48, .note = "SCK; also the board LED, lit by 1" },
	{ .name = "A0", .gpio = 1 },
	{ .name = "A1", .gpio = 2 },
	{ .name = "A2", .gpio = 3 },
	{ .name = "A3", .gpio = 4 },
	{ .name = "A4", .gpio = 11 },
	{ .name = "A5", .gpio = 12 },
	{ .name = "A6", .gpio = 13 },
	{ .name = "A7", .gpio = 14 },
	/* The RGB LED is three LEDs sharing an anode, so a pin held LOW lights its
	 * colour -- the opposite sense to the board LED on D13. Arduino's own
	 * numbering calls these three D14, D15 and D16. */
	{ .name = "LED_RED", .gpio = 46, .note = "red RGB channel, lit by 0; strapping pin" },
	{ .name = "LED_GREEN", .gpio = 0, .note = "green RGB channel, lit by 0; BOOT pin" },
	{ .name = "LED_BLUE", .gpio = 45, .note = "blue RGB channel, lit by 0; strapping pin" },
	{ .name = "D14", .gpio = 46 },
	{ .name = "D15", .gpio = 0 },
	{ .name = "D16", .gpio = 45 },
	{ .name = "LED_R", .gpio = 46 },
	{ .name = "LED_G", .gpio = 0 },
	{ .name = "LED_B", .gpio = 45 },
	/* Arduino's variant defines `LED_BUILTIN = D13`, the pin the user LED has
	 * been on since the Uno, so the board's LED and D13 are one line. */
	{ .name = "LED_BOARD", .gpio = 48, .note = "the yellow board LED, lit by 1" },
};

#define MAX_PINS 128
static struct pin_name pins[MAX_PINS];
static int n_pins;
static char pin_source[MAX_PINS];	/* 'b' built-in, 'f' from the file */

/* Strapping pins. What these read while the chip is resetting decides the boot
 * mode, so a level driven here lasts until the next reset and can then change
 * how the board starts -- and GPIO0 is also the BOOT button, which pulls it low
 * by hand. The three are the same three the RGB LED is on, which is not a
 * coincidence: the LED was put on them because they are already pulled and
 * already known at reset. */
static const int strapping_pins[] = { 0, 45, 46 };

static int is_strapping(int gpio)
{
	size_t i;

	for (i = 0; i < sizeof(strapping_pins) / sizeof(strapping_pins[0]); i++) {
		if (strapping_pins[i] == gpio)
			return 1;
	}
	return 0;
}

#define PINS_FILE "/etc/espctl.pins"

static int load_pins(void)
{
	FILE *file;
	char line[128];
	size_t i;

	for (i = 0; i < sizeof(builtin_pins) / sizeof(builtin_pins[0]); i++) {
		pins[n_pins] = builtin_pins[i];
		pin_source[n_pins] = 'b';
		n_pins++;
	}

	/* /etc/espctl.pins extends and overrides the table above, so a name can
	 * be added on the board -- /etc survives a reflash -- without a rebuild.
	 * A missing file is the normal case and is not an error. */
	file = fopen(PINS_FILE, "r");
	if (!file)
		return 0;

	while (fgets(line, sizeof(line), file)) {
		char *name = line, *value, *end;
		long gpio;

		while (*name == ' ' || *name == '\t')
			name++;
		if (*name == '#' || *name == '\n' || *name == '\0')
			continue;

		value = strchr(name, '=');
		if (!value)
			continue;
		*value++ = '\0';

		end = name + strlen(name);
		while (end > name && (end[-1] == ' ' || end[-1] == '\t'))
			*--end = '\0';

		errno = 0;
		gpio = strtol(value, &end, 10);
		if (errno || end == value || gpio < 0 || gpio > 1024)
			continue;
		if (name[0] == '\0')
			continue;

		for (i = 0; i < (size_t) n_pins; i++) {
			if (strcmp(pins[i].name, name) != 0)
				continue;
			/* The note describes where the name pointed before, so an
			 * override drops it rather than let it describe a pin the
			 * name no longer refers to. */
			pins[i].gpio = (int) gpio;
			pins[i].note = NULL;
			pin_source[i] = 'f';
			break;
		}
		if (i == (size_t) n_pins && n_pins < MAX_PINS) {
			pins[n_pins].name = strdup(name);
			if (!pins[n_pins].name)
				break;
			pins[n_pins].gpio = (int) gpio;
			pins[n_pins].note = NULL;
			pin_source[n_pins] = 'f';
			n_pins++;
		}
	}

	fclose(file);
	return 0;
}

static int pin_lookup(const char *word, int *gpio)
{
	char *end;
	long value;
	int i;

	for (i = 0; i < n_pins; i++) {
		if (strcmp(pins[i].name, word) == 0) {
			*gpio = pins[i].gpio;
			return 0;
		}
	}

	/* A bare number is accepted only if it names a pin the board actually
	 * has -- either in the table or ruled in by /etc/espctl.pins. This is
	 * what keeps `espctl gpio set 27 1` (flash) and 39-42 (JTAG) out. */
	errno = 0;
	value = strtol(word, &end, 10);
	if (errno || end == word || *end != '\0' || value < 0 || value > 1024)
		return -1;

	for (i = 0; i < n_pins; i++) {
		if (pins[i].gpio == (int) value) {
			*gpio = (int) value;
			return 0;
		}
	}
	return -1;
}

static const char *pin_names_for(int gpio, char *buffer, size_t size)
{
	int i;
	size_t used = 0;

	buffer[0] = '\0';
	for (i = 0; i < n_pins; i++) {
		if (pins[i].gpio != gpio || pin_source[i] == 'f')
			continue;
		used += (size_t) snprintf(buffer + used, used < size ? size - used : 0,
					  "%s%s", used ? "/" : "", pins[i].name);
		if (used >= size)
			break;
	}
	return buffer;
}

static void print_pins(void)
{
	char names[128];
	int i, j;

	printf("  %-10s %-4s %s\n", "NAME", "GPIO", "SOURCE");
	for (i = 0; i < n_pins; i++)
		printf("  %-10s %-4d %s\n", pins[i].name, pins[i].gpio,
		       pin_source[i] == 'f' ? PINS_FILE : "built-in");

	/* Aliases share a line, so say which names belong together rather than
	 * leaving the reader to spot the repeats: D13 and LED_BOARD are the same
	 * pin, as are the three spellings of each RGB channel. Reported once per
	 * line, at its first name, so the list stays sorted as the table is. */
	printf("\n  A line can have several names; `gpio set` takes any of them.\n");
	for (i = 0; i < n_pins; i++) {
		int names_here = 0, first = 1;

		if (pin_source[i] == 'f')
			continue;
		for (j = 0; j < n_pins; j++) {
			if (pin_source[j] == 'f' || pins[j].gpio != pins[i].gpio)
				continue;
			names_here++;
			if (j < i)
				first = 0;
		}
		if (names_here > 1 && first) {
			pin_names_for(pins[i].gpio, names, sizeof(names));
			printf("  gpio %d is %s\n", pins[i].gpio, names);
		}
	}

	/* What the header prints and what the line does at reset. Only the first
	 * name of a pin carries its note, so an alias cannot repeat it. */
	printf("\n  Notes:\n");
	for (i = 0; i < n_pins; i++) {
		if (!pins[i].note)
			continue;
		for (j = 0; j < i; j++) {
			if (pins[j].gpio == pins[i].gpio && pins[j].note)
				break;
		}
		if (j < i)
			continue;
		printf("  %-10s %s\n", pins[i].name, pins[i].note);
	}

	/* Named rather than left out silently, because "my pin is missing from
	 * this list" and "this pin cannot be used" are different answers and only
	 * one of them means the tool is wrong. */
	printf("\n  Not offered: D0 and D1, which are UART0's RX and TX, and this\n"
	       "    kernel's command line sets console=ttyS0 on them;\n"
	       "    gpio 19/20 (the native USB the console comes out of), 26 (PSRAM\n"
	       "    chip select), 27-37 (flash and octal PSRAM), 39-42 (JTAG).\n"
	       "\n"
	       "  The kernel enforces none of that: every one of those lines claims\n"
	       "    cleanly through sysfs, so this list is the only guard.\n"
	       "\n"
	       "  The three RGB lines are driven dark at boot by /etc/init.d/S04led,\n"
	       "    which then gives them back. The level outlives the line being\n"
	       "    freed -- this chip's GPIO driver installs no .free hook, so\n"
	       "    releasing a line writes nothing to the pad -- so the LED stays\n"
	       "    dark with nothing holding the pins, and `gpio set LED_BLUE 1`\n"
	       "    works straight after boot. No service to stop first.\n");
}

/* The number in a /dev/gpiochipN name, or -1 if the entry is not one. The
 * number is also what the caller builds the path back out of, rather than
 * copying the name it came from.
 *
 * That makes the name's spelling load-bearing, so it is required here to be
 * the one rendering of its own number. `gpiochip07` parses to 7 and would be
 * opened as `/dev/gpiochip7`, which does not exist -- and a chip that will not
 * open is reported below as a board with no GPIO, a wrong answer rather than a
 * missing one. The kernel names these with a plain %d and never zero-pads, so
 * nothing real is turned away. */
static int chip_number(const char *name)
{
	char canonical[24];
	char *end;
	long value;

	if (strncmp(name, "gpiochip", 8) != 0 || name[8] == '\0')
		return -1;
	errno = 0;
	value = strtol(name + 8, &end, 10);
	if (errno || *end != '\0' || value < 0 || value > INT_MAX)
		return -1;
	snprintf(canonical, sizeof(canonical), "gpiochip%d", (int) value);
	if (strcmp(canonical, name) != 0)
		return -1;
	return (int) value;
}

static int find_chip(char *path, size_t size, char *label, size_t label_size,
		     int *lines)
{
	const char *override = getenv("ESPCTL_GPIOCHIP");
	struct gpiochip_info info;
	struct dirent *entry;
	int first = -1, first_fd = -1, scanned = 0;
	char candidate[64];
	DIR *dir;

	if (override && *override) {
		int fd = open(override, O_RDWR);
		if (fd < 0) {
			perror(override);
			return -1;
		}
		if (ioctl(fd, GPIO_GET_CHIPINFO_IOCTL, &info) < 0) {
			perror("GPIO_GET_CHIPINFO_IOCTL");
			close(fd);
			return -1;
		}
		snprintf(path, size, "%s", override);
		snprintf(label, label_size, "%s", info.label);
		*lines = (int) info.lines;
		return fd;
	}

	dir = opendir("/dev");
	if (!dir) {
		perror("/dev");
		return -1;
	}
	while ((entry = readdir(dir)) != NULL && scanned < GPIO_MAX_CHIPS) {
		int number = chip_number(entry->d_name);
		int fd;

		if (number < 0)
			continue;
		scanned++;

		/* Rendered from the number, not copied from the entry: copying is
		 * what GCC's -Wformat-truncation rejects, and rightly, since a
		 * directory entry is 256 bytes going into a 64-byte path. The
		 * number is already known to render back to this exact name. */
		snprintf(candidate, sizeof(candidate), "/dev/gpiochip%d", number);
		fd = open(candidate, O_RDWR);
		if (fd < 0)
			continue;
		if (ioctl(fd, GPIO_GET_CHIPINFO_IOCTL, &info) < 0) {
			close(fd);
			continue;
		}
		if (strcmp(info.label, GPIO_LABEL) == 0) {
			if (first_fd >= 0)
				close(first_fd);
			closedir(dir);
			snprintf(path, size, "%s", candidate);
			snprintf(label, label_size, "%s", info.label);
			*lines = (int) info.lines;
			return fd;
		}
		if (first_fd < 0 || number < first) {
			if (first_fd >= 0)
				close(first_fd);
			first = number;
			first_fd = fd;
		} else {
			close(fd);
		}
	}
	closedir(dir);

	/* No chip carries the expected label. Say what was there instead, which
	 * is the difference between "no GPIO at all" and "the label moved". */
	if (first_fd >= 0) {
		snprintf(path, size, "/dev/gpiochip%d", first);
		fprintf(stderr, "espctl: no gpiochip labelled %s (found: %s, ", GPIO_LABEL, path);
		if (ioctl(first_fd, GPIO_GET_CHIPINFO_IOCTL, &info) == 0)
			fprintf(stderr, "label %s, %u lines)\n", info.label, info.lines);
		else
			fprintf(stderr, "label unreadable)\n");
		close(first_fd);
	} else {
		fprintf(stderr, "espctl: no /dev/gpiochip* device at all\n");
	}
	return -1;
}

static int gpio_request_line(int chip_fd, int pin, __u64 direction_flag, int value)
{
	struct gpio_v2_line_request req;
	int ret;

	memset(&req, 0, sizeof(req));
	req.num_lines = 1;
	req.offsets[0] = (__u32) pin;
	strncpy(req.consumer, "espctl", sizeof(req.consumer) - 1);
	req.config.flags = direction_flag;

	if (direction_flag & GPIO_V2_LINE_FLAG_OUTPUT) {
		req.config.num_attrs = 1;
		req.config.attrs[0].attr.id = GPIO_V2_LINE_ATTR_ID_OUTPUT_VALUES;
		req.config.attrs[0].attr.values = value ? 1 : 0;
		req.config.attrs[0].mask = 1;
	}

	ret = ioctl(chip_fd, GPIO_V2_GET_LINE_IOCTL, &req);
	if (ret < 0)
		return -1;
	return req.fd;
}

/* $1 = a name or a number, $2 = the callback's own description of it */
static int resolve(const char *word, int *gpio, char *names, size_t names_size)
{
	if (pin_lookup(word, gpio) < 0) {
		char buffer[128];

		fprintf(stderr, "espctl: '%s' is not a pin on this board\n", word);
		if (pin_names_for(atoi(word), buffer, sizeof(buffer))[0] != '\0')
			fprintf(stderr, "espctl: gpio %d is %s\n", atoi(word), buffer);
		fprintf(stderr, "espctl: `espctl gpio list` shows the names\n");
		return -1;
	}
	pin_names_for(*gpio, names, names_size);
	return 0;
}

static int with_chip(char *path, size_t size, char *label, size_t label_size,
		     int *lines, int *chip_fd)
{
	*chip_fd = find_chip(path, size, label, label_size, lines);
	return *chip_fd < 0 ? -1 : 0;
}

static int cmd_gpio_set(const char *word, int value)
{
	struct gpio_v2_line_values vals;
	char names[128], path[64], label[64];
	int pin, chip_fd, line_fd, lines;

	if (resolve(word, &pin, names, sizeof(names)) < 0)
		return 1;
	if (with_chip(path, sizeof(path), label, sizeof(label), &lines, &chip_fd) < 0)
		return 1;
	if (pin >= lines) {
		fprintf(stderr, "espctl: gpio %d is past the %d lines of %s\n", pin, lines, path);
		close(chip_fd);
		return 1;
	}

	line_fd = gpio_request_line(chip_fd, pin, GPIO_V2_LINE_FLAG_OUTPUT, value);
	close(chip_fd);
	if (line_fd < 0) {
		/* A line is claimable once, so "busy" means somebody else has it --
		 * another driver, or a line exported through sysfs. Not this image's
		 * LED pins any more: S04led drives them at boot and immediately
		 * gives them back, and the level outlives the line, so they are free
		 * by the time anyone logs in. */
		fprintf(stderr, "espctl: could not claim gpio %d (%s): %s\n",
			pin, names[0] ? names : "-", strerror(errno));
		if (errno == EBUSY)
			fprintf(stderr, "espctl: gpio %d is already claimed -- by a "
				"driver, or by a line exported through /sys/class/gpio\n",
				pin);
		return 1;
	}

	memset(&vals, 0, sizeof(vals));
	vals.mask = 1;
	vals.bits = value ? 1 : 0;
	if (ioctl(line_fd, GPIO_V2_LINE_SET_VALUES_IOCTL, &vals) < 0) {
		perror("GPIO_V2_LINE_SET_VALUES_IOCTL");
		close(line_fd);
		return 1;
	}

	printf("gpio %d %s= %d\n", pin, names[0] ? "" : " ", value ? 1 : 0);
	if (names[0])
		printf("  %s\n", names);
	if (is_strapping(pin))
		printf("  strapping pin: what it reads at the next reset picks the"
		       " boot mode\n");
	close(line_fd);
	return 0;
}

static int cmd_gpio_get(const char *word)
{
	struct gpio_v2_line_values vals;
	char names[128], path[64], label[64];
	int pin, chip_fd, line_fd, lines;

	if (resolve(word, &pin, names, sizeof(names)) < 0)
		return 1;
	if (with_chip(path, sizeof(path), label, sizeof(label), &lines, &chip_fd) < 0)
		return 1;
	if (pin >= lines) {
		fprintf(stderr, "espctl: gpio %d is past the %d lines of %s\n", pin, lines, path);
		close(chip_fd);
		return 1;
	}

	line_fd = gpio_request_line(chip_fd, pin, GPIO_V2_LINE_FLAG_INPUT, 0);
	close(chip_fd);
	if (line_fd < 0) {
		fprintf(stderr, "espctl: could not claim gpio %d: %s\n", pin, strerror(errno));
		return 1;
	}

	memset(&vals, 0, sizeof(vals));
	vals.mask = 1;
	if (ioctl(line_fd, GPIO_V2_LINE_GET_VALUES_IOCTL, &vals) < 0) {
		perror("GPIO_V2_LINE_GET_VALUES_IOCTL");
		close(line_fd);
		return 1;
	}

	printf("gpio %d = %d\n", pin, (int) (vals.bits & 1));
	if (names[0])
		printf("  %s\n", names);
	close(line_fd);
	return 0;
}

static int read_first_line(const char *path, char *buffer, size_t size)
{
	FILE *file = fopen(path, "r");

	if (!file)
		return -1;
	if (!fgets(buffer, (int) size, file)) {
		fclose(file);
		return -1;
	}
	fclose(file);
	buffer[strcspn(buffer, "\n")] = '\0';
	return 0;
}

/* The frequency has no live source on this image: there is no cpufreq driver
 * and no governor, and the project's own clock driver registers cpu_clk
 * through clk_hw_register_fixed_rate(), so the rate is fixed for the life of a
 * boot. Two places can report it, and debugfs is the better one. It is off in
 * this kernel (CONFIG_DEBUG_FS is not set), so in practice the value comes
 * from /proc/cpuinfo, which the board does print. */
static int cpu_freq_mhz(double *mhz)
{
	char line[256];
	FILE *file;

	if (read_first_line("/sys/kernel/debug/clk/cpu_clk/clk_rate", line, sizeof(line)) == 0) {
		char *end;
		double hz = strtod(line, &end);

		if (end != line && hz > 0) {
			*mhz = hz / 1e6;
			return 0;
		}
	}

	file = fopen("/proc/cpuinfo", "r");
	if (!file)
		return -1;
	while (fgets(line, sizeof(line), file)) {
		char *colon, *value, *end;
		double v;

		if (strncmp(line, "cpu MHz", 7) != 0)
			continue;
		colon = strchr(line, ':');
		if (!colon)
			continue;
		value = colon + 1;
		while (*value == ' ' || *value == '\t')
			value++;
		v = strtod(value, &end);
		if (end != value && v > 0) {
			*mhz = v;
			fclose(file);
			return 0;
		}
	}
	fclose(file);
	return -1;
}

static int cmd_gpio_list(void)
{
	print_pins();
	return 0;
}

static int cmd_i2c_scan(const char *bus_path)
{
	int fd, addr;

	fd = open(bus_path, O_RDWR);
	if (fd < 0) {
		perror(bus_path);
		return 1;
	}

	printf("     0  1  2  3  4  5  6  7  8  9  a  b  c  d  e  f\n");
	for (addr = 0; addr < 0x80; addr++) {
		if (addr % 16 == 0)
			printf("%02x: ", addr);

		if (addr < 0x03 || addr > 0x77) {
			printf("   ");
		} else if (ioctl(fd, I2C_SLAVE, addr) < 0) {
			printf("   ");
		} else {
			/* One real byte, not a zero-length read: a zero-length
			 * read returns 0 without touching the bus, which reported
			 * every address in 0x03-0x77 as present. The probe is
			 * "absent only if the transfer was refused with ENXIO";
			 * anything else means something answered, including the
			 * EREMOTEIO a write-only device gives to a read. */
			unsigned char dummy;
			int got = (int) read(fd, &dummy, 1);

			if (got >= 0 || errno != ENXIO)
				printf("%02x ", addr);
			else
				printf("-- ");
		}

		if (addr % 16 == 15)
			printf("\n");
	}

	close(fd);
	return 0;
}

/* The S3's own temperature sensor, which the kernel exposes through hwmon. The
 * device is found by the name its driver registers rather than assumed to be
 * hwmon0: the number is registration order and nothing promises this image has
 * only one hwmon device, so a hardcoded path would be a fact that happens to be
 * true today. When it is not there at all -- CONFIG_HWMON off, or the driver's
 * probe failed and dmesg says why -- the reading is left out rather than filled
 * with something.
 *
 * A read is not free. The driver powers the shared SENS block up, samples and
 * powers it down again for each one, because core 0's ESP-IDF owns the same
 * silicon, so this line costs the one measurement it prints. */
#define TEMP_SENSOR_NAME "esp32s3_tsens"
#define TEMP_READING "temp1_input"

static int cpu_temp_mc(long *mc)
{
	char path[PATH_MAX], name[64], value[64];
	struct dirent *ent;
	DIR *dir;
	char *end;

	dir = opendir("/sys/class/hwmon");
	if (!dir)
		return -1;

	while ((ent = readdir(dir)) != NULL) {
		if (strncmp(ent->d_name, "hwmon", 5) != 0)
			continue;
		snprintf(path, sizeof(path), "/sys/class/hwmon/%s/name",
			 ent->d_name);
		if (read_first_line(path, name, sizeof(name)) != 0)
			continue;
		if (strcmp(name, TEMP_SENSOR_NAME) != 0)
			continue;

		snprintf(path, sizeof(path), "/sys/class/hwmon/%s/" TEMP_READING,
			 ent->d_name);
		closedir(dir);
		if (read_first_line(path, value, sizeof(value)) != 0)
			return -1;
		*mc = strtol(value, &end, 10);
		return end == value ? -1 : 0;
	}

	closedir(dir);
	return -1;
}

/*
 * The board's own identity, which is the device tree and nothing else: an
 * ESP32-S3 has no board ID EEPROM. The root node carries
 *
 *	model = "Arduino Nano ESP32";
 *	compatible = "arduino,nano-esp32", "espressif,esp32s3";
 *
 * where `model` is the board's full name and `compatible` names the board and
 * then the chip. These are bare property values -- NUL-terminated, with no
 * trailing newline -- so they are read as bytes and not as lines, and a
 * `compatible` list is walked entry by entry because a C string stops at the
 * first NUL and would otherwise never reach the chip.
 *
 * DT_BASE is a macro so the parsing below can be exercised against a directory
 * of sample properties away from the board.
 */
#ifndef DT_BASE
#define DT_BASE "/sys/firmware/devicetree/base/"
#endif

static int dt_property(const char *name, char *buf, size_t len, size_t *nbytes)
{
	char path[PATH_MAX];
	FILE *file;
	size_t n;

	snprintf(path, sizeof(path), DT_BASE "%s", name);
	file = fopen(path, "r");
	if (!file)
		return -1;
	n = fread(buf, 1, len - 1, file);
	fclose(file);
	buf[n] = '\0';
	*nbytes = n;
	return 0;
}

/* The last NUL-separated string in a property. A `compatible` list runs
 * most-specific first, so the chip is the entry at the end. */
static const char *dt_last_entry(const char *value, size_t nbytes)
{
	const char *last = value;
	size_t off = 0;

	while (off < nbytes) {
		size_t l = strnlen(value + off, nbytes - off);

		if (l == 0)
			break;
		last = value + off;
		off += l + 1;
	}
	return last;
}

/*
 * A part number as Espressif writes it: upper case, with a hyphen setting the
 * variant apart from the rest. The device tree spells the chip `esp32s3` and
 * the part's own name is `ESP32-S3`, so the letter prefix and the digit run
 * are read off and whatever follows them is separated -- `esp32` alone stays
 * `ESP32`, and `esp32c3` would read `ESP32-C3`. No part is spelled out here.
 */
static void soc_part(const char *in, char *out, size_t len)
{
	size_t n = strnlen(in, len - 1), i;

	for (i = 0; i < n; i++)
		out[i] = (char)toupper((unsigned char)in[i]);
	out[n] = '\0';

	i = 0;
	while (i < n && isalpha((unsigned char)out[i]))
		i++;
	while (i < n && isdigit((unsigned char)out[i]))
		i++;
	if (i > 0 && i < n) {
		memmove(out + i + 1, out + i, n - i + 1);
		out[i] = '-';
	}
}

/*
 * The chip as "maker part", both from the last entry of the root `compatible`
 * list -- `espressif,esp32s3`. The maker is that entry's vendor prefix,
 * capitalised, because a devicetree prefix is lower case by rule. If the list
 * names only the board then there is no chip entry, and this fails rather than
 * passing the board's own name off as the chip's.
 */
static int soc_name(char *out, size_t len)
{
	char buf[256], maker[32], part[64];
	const char *entry, *comma;
	size_t n, l;

	if (dt_property("compatible", buf, sizeof(buf), &n) != 0)
		return -1;
	entry = dt_last_entry(buf, n);
	if (entry == buf)
		return -1;
	if ((comma = strchr(entry, ',')) == NULL)
		return -1;
	l = (size_t)(comma - entry);
	if (l == 0 || l >= sizeof(maker))
		return -1;
	memcpy(maker, entry, l);
	maker[l] = '\0';
	maker[0] = (char)toupper((unsigned char)maker[0]);

	soc_part(comma + 1, part, sizeof(part));
	snprintf(out, len, "%s %s", maker, part);
	return 0;
}

static int cmd_system_info(void)
{
	struct utsname uts;
	struct sysinfo si;
	char path[64], label[64];
	char dtbuf[256];
	size_t dtn = 0, vl;
	double mhz;
	long mc;
	int chip_fd, lines, i, from_file;

	if (uname(&uts) == 0) {
		printf("kernel:    %s %s\n", uts.sysname, uts.release);
		printf("machine:   %s\n", uts.machine);
	}

	/* "Arduino Nano ESP32": the maker, then the model name. */
	if (dt_property("model", dtbuf, sizeof(dtbuf), &dtn) == 0 && dtn > 0) {
		printf("board:     %s\n", dtbuf);
		for (vl = 0; vl < dtn && dtbuf[vl] != ' '; vl++)
			;
		if (vl > 0 && vl < dtn) {
			dtbuf[vl] = '\0';
			printf("vendor:    %s\n", dtbuf);
		}
	}

	if (soc_name(dtbuf, sizeof(dtbuf)) == 0)
		printf("soc:       %s\n", dtbuf);

	if (sysinfo(&si) == 0) {
		printf("uptime:    %ld s\n", si.uptime);
		printf("mem total: %lu KB\n", si.totalram * si.mem_unit / 1024);
		printf("mem free:  %lu KB\n", si.freeram * si.mem_unit / 1024);
	}

	chip_fd = find_chip(path, sizeof(path), label, sizeof(label), &lines);
	if (chip_fd >= 0) {
		printf("gpio:      %s (%s, %d lines)\n", path, label, lines);
		close(chip_fd);
	}
	/* Which of the two tables the names came from, not which files exist.
	 * PINS_FILE used to be named here unconditionally, so a board without it
	 * -- the normal case, and this one -- was told its 30 names came from a
	 * file that was never opened. The count is what matters and the source is
	 * what makes it checkable, so both are reported and only real sources are
	 * named. */
	for (i = 0, from_file = 0; i < n_pins; i++) {
		if (pin_source[i] == 'f')
			from_file++;
	}
	if (from_file)
		printf("pins:      %d names (%d built-in, %d from %s)\n",
		       n_pins, n_pins - from_file, from_file, PINS_FILE);
	else
		printf("pins:      %d names (built-in)\n", n_pins);

	if (cpu_freq_mhz(&mhz) == 0)
		printf("cpu freq:  %.2f MHz\n", mhz);

	if (cpu_temp_mc(&mc) == 0)
		printf("temp:      %.1f C\n", mc / 1000.0);

	return 0;
}

static void usage(const char *argv0)
{
	fprintf(stderr,
		"usage: %s gpio list\n"
		"       %s gpio set <name|gpio> <0|1>\n"
		"       %s gpio get <name|gpio>\n"
		"       %s i2c scan [/dev/i2c-N]\n"
		"       %s system info\n"
		"\n"
		"Pin names are the Nano's own: D0-D13, A0-A7 and LED_RED/LED_GREEN/\n"
		"LED_BLUE (with LED_R/LED_G/LED_B and LED_BOARD as aliases). A bare\n"
		"GPIO number works for any pin the board breaks out. Names can be\n"
		"added or overridden in " PINS_FILE "; `gpio list` shows which came\n"
		"from there.\n"
		"\n"
		"The RGB LED is common-anode, so 0 lights a colour and 1 is dark.\n",
		argv0, argv0, argv0, argv0, argv0);
}

int main(int argc, char **argv)
{
	load_pins();

	if (argc < 2 || !strcmp(argv[1], "-h") || !strcmp(argv[1], "--help") ||
	    !strcmp(argv[1], "help")) {
		usage(argv[0]);
		return argc < 2 ? 1 : 0;
	}

	if (!strcmp(argv[1], "gpio")) {
		if (argc == 3 && !strcmp(argv[2], "list"))
			return cmd_gpio_list();
		if (argc == 5 && !strcmp(argv[2], "set"))
			return cmd_gpio_set(argv[3], atoi(argv[4]) ? 1 : 0);
		if (argc == 4 && !strcmp(argv[2], "get"))
			return cmd_gpio_get(argv[3]);
	} else if (!strcmp(argv[1], "i2c")) {
		if (argc >= 3 && !strcmp(argv[2], "scan"))
			return cmd_i2c_scan(argc > 3 ? argv[3] : "/dev/i2c-0");
	} else if (!strcmp(argv[1], "system")) {
		if (argc == 3 && !strcmp(argv[2], "info"))
			return cmd_system_info();
	}

	usage(argv[0]);
	return 1;
}
