#!/usr/bin/env python3
"""The panel is a console, and the four places that fact lives agree.

A framebuffer console is not one change. It is a kernel config, a kernel
command line, an inittab line and a partition table, and each of them fails
differently and silently:

  - without CONFIG_FONTS, FONT_6x8 is dropped and the console comes up in
    8x16, which is 16 columns on a 128-pixel panel -- readable as a bug only by
    looking at the screen;
  - without console=tty1 the kernel prints to UART0 only and the panel shows
    nothing until a getty writes to it;
  - without the inittab line there is no login prompt on the panel, only kernel
    messages;
  - and the console does not fit the `linux` partition without the 64K the
    partition table moves to it, which package-final.py catches only at the end
    of a build.

So this checks the four together, plus the two things that must NOT change:
the serial console stays, and /dev/fb0 stays a canvas any program can open.
"""
import re
import struct
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOARD = ROOT / 'new-files/board/espressif/esp32s3'
CONFIG = BOARD / 'nano_esp32_linux.config'
TRIM = BOARD / 'trim-target.sh'
PATCHES = BOARD / 'patches/linux'
CSV = ROOT / 'new-files/esp-hosted/network_adapter/partition_table.esp32s3.16m8r'
TABLE_BIN = ROOT / 'images/partition-table.bin'
PACKAGE = ROOT / 'build/package-final.py'


def settings(text):
    """Every CONFIG_ line as a symbol -> value map, comments included."""
    found = {}
    for line in text.splitlines():
        line = line.strip()
        match = re.match(r'^(# )?(CONFIG_[A-Za-z0-9_]+)(?:=(.*)| is not set)$', line)
        if match:
            found[match.group(2)] = match.group(3) if match.group(3) is not None else 'n'
    return found


def partitions(text):
    """The CSV as an ordered list of (name, offset, size)."""
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        fields = [field.strip() for field in line.split(',')]
        rows.append((fields[0], int(fields[-2], 16), int(fields[-1], 16)))
    return rows


def binary_table(path):
    """The generated table, decoded the way the kernel reads it."""
    data = path.read_bytes()
    rows = {}
    for pos in range(0, len(data) - 31, 32):
        magic, _, _, offset, size, label, _ = struct.unpack_from('<HBBII16sI', data, pos)
        if magic != 0x50AA:
            break
        rows[label.split(b'\0', 1)[0].decode('ascii')] = (offset, size)
    return rows


class ConsoleConfigTests(unittest.TestCase):
    def setUp(self):
        self.config = settings(CONFIG.read_text())

    def test_the_vt_layer_and_fbcon_are_on(self):
        for symbol in ('CONFIG_VT', 'CONFIG_VT_CONSOLE', 'CONFIG_FRAMEBUFFER_CONSOLE'):
            with self.subTest(symbol=symbol):
                self.assertEqual(self.config.get(symbol), 'y',
                                 symbol + ' is what puts text on the panel')

    def test_the_font_menu_is_on(self):
        # FONTS gates every font below it and nothing selects it, so a seed that
        # names FONT_6x11 without FONTS gets 8x16 and no warning.
        self.assertEqual(self.config.get('CONFIG_FONTS'), 'y',
                         'without CONFIG_FONTS, FONT_6x11 is dropped silently')

    def test_the_font_is_the_one_that_fits_a_240_pixel_panel(self):
        # 6x11 gives 40x21 on a 240x240 panel: the same 40 columns 6x8 had,
        # with glyphs a third taller. 6x8 was the earlier choice, made for the
        # 128x160 panel where 40 columns did not fit; at 240 pixels wide that
        # constraint is gone and the height is free to spend on legibility.
        #
        # 6x11 is the only font linked, which matters because fbcon's own
        # scoring would not pick it: get_default_font() (lib/fonts/fonts.c:342)
        # gives a +1000 bonus only to fonts at most 8 rows tall when yres < 400,
        # and 240 is, so a linked 6x8 would win on score alone. Patch 0067 also
        # names the font on the cmdline (`fbcon=font:ProFont6x11`) so the choice
        # survives a second font being linked later.
        self.assertEqual(self.config.get('CONFIG_FONT_6x11'), 'y')
        self.assertEqual(self.config.get('CONFIG_FONT_6x8'), 'n',
                         'FONT_6x8 scores higher than 6x11 on a 240-row panel; '
                         'linking it would silently win and give 40x30')
        self.assertEqual(self.config.get('CONFIG_FONT_8x16'), 'n',
                         'FONT_8x16 is default y; leaving it on links a second '
                         'font that is never used and is 16 columns wide')

    def test_the_cmdline_names_the_font(self):
        # With one font linked the cmdline is belt and braces, but it is what
        # keeps the choice explicit: fbcon falls back to get_default_font()
        # when the name is absent OR when it names a font that is not linked,
        # and that fallback would pick 6x8 if one were ever added back.
        text = PATCHES.joinpath(
            '0067-esp32s3-the-panel-becomes-a-console.patch').read_text()
        added = [line for line in text.splitlines()
                 if line.startswith('+') and 'bootargs' in line]
        self.assertEqual(len(added), 1, added)
        self.assertIn('fbcon=font:ProFont6x11', added[0],
                      'the font name is the one font_6x11.c registers '
                      '(.name = "ProFont6x11"), not the Kconfig symbol')

    def test_the_unicode_tables_are_not_paid_for(self):
        # A 4x6 font has no glyphs above U+00FF, and the stubs left behind pass
        # ASCII through unchanged. ~15 KB of xipImage.
        self.assertEqual(self.config.get('CONFIG_CONSOLE_TRANSLATIONS'), 'n')

    def test_the_framebuffer_stays_a_canvas(self):
        for symbol in ('CONFIG_FB', 'CONFIG_FB_DEVICE', 'CONFIG_FB_TFT',
                       'CONFIG_FB_TFT_ST7789V'):
            with self.subTest(symbol=symbol):
                self.assertEqual(self.config.get(symbol), 'y',
                                 'the console is added to /dev/fb0, not instead of it')

    def test_the_st7735_driver_is_gone(self):
        # The panel is a 240x240 ST7789 now. Both drivers could be linked in
        # at once -- nothing in the Kconfig makes them exclusive -- but a
        # second fbtft driver is dead code in xipImage and a second
        # `compatible` string that could bind the wrong panel.
        #
        # Written as an explicit "is not set" rather than left out, because
        # check-kernel-config.py only verifies symbols the seed names: a
        # symbol that is absent from the seed is never compared against the
        # built .config, so leaving it out would not catch olddefconfig
        # turning it back on.
        self.assertEqual(self.config.get('CONFIG_FB_TFT_ST7735R'), 'n',
                         'the ST7735R driver has to be off, and named in the '
                         'seed so the config check looks at it')

    def test_the_panel_geometry_comes_from_the_devicetree(self):
        # fb_st7789v.c's own display struct is 240x320: 320 is the
        # controller's column count, not this module's. fbtft takes width and
        # height from the driver unless the node overrides them, so without
        # these two the framebuffer is 80 rows too tall and fbcon lays out
        # 30 rows of text that are not on the glass.
        patch = PATCHES.joinpath(
            '0063-esp32s3-dts-nano-pin-map-st7789-panel-temp-node.patch').read_text()
        self.assertIn('compatible = "sitronix,st7789v";', patch)
        self.assertIn('width  = <240>;', patch)
        self.assertIn('height = <240>;', patch)
        # fbtft_properties_read() reads bare `width`/`height` through
        # device_property_read_u32; a `fbtft,` prefix would be read as absent
        # and silently ignored, which is the failure this asserts against.
        # The property form, not the bare string: the comment above the two
        # lines names `fbtft,width` to say why it is wrong.
        self.assertNotIn('fbtft,width  = <', patch)

    def test_the_row_offset_is_in_the_node_and_settable_at_runtime(self):
        # The controller's GRAM is 240x320 and this module shows 240x240 of
        # it. Which 240 rows are wired to glass is a property of the module's
        # own wiring and cannot be read out of it -- it has to be tried on the
        # board. The board answered: 0. This module is top-aligned, visible
        # rows 0..239, and the image is flush to the top edge with the bottom
        # line unclipped. Adafruit's 80 (`_rowstart = 320 - height`) assumes
        # bottom alignment and does not hold here; the first boot with 80 left
        # the top third of the glass blank.
        #
        # Patch 0069 adds a sysfs knob beside the property so the answer can be
        # changed with `echo` rather than by rebuilding a kernel that takes
        # hours under emulation -- which is how 0 was found.
        dts = PATCHES.joinpath(
            '0063-esp32s3-dts-nano-pin-map-st7789-panel-temp-node.patch').read_text()
        self.assertIn('row-offset = <0>;', dts)
        # Bare name, like width and height: fbtft_property_value() is
        # device_property_read_u32() with no vendor prefix, so a `fbtft,`
        # spelling would be read as absent and silently ignored.
        self.assertNotIn('fbtft,row-offset', dts)

        drv = PATCHES.joinpath(
            '0069-fbtft-st7789v-panel-geometry-knobs.patch').read_text()
        # The property is parsed, and the sysfs attribute is created beside
        # the existing `debug` one.
        self.assertIn('fbtft_property_value(dev, "row-offset")', drv)
        self.assertIn('__ATTR(row_offset, 0660, show_row_offset, store_row_offset)',
                      drv)
        # set_addr_win is what applies it: fbtft's own always starts the
        # window at row 0, and fbtft_update_display() passes the same
        # start_line to write_vmem as a byte offset, so shifting the window
        # shifts the image and nothing else.
        self.assertIn('set_addr_win = set_addr_win', drv)
        self.assertIn('ys += par->row_offset;', drv)

    def test_the_panel_is_driven_in_spi_mode3(self):
        # The Adafruit sketch that brought this panel up passes SPI_MODE3 to
        # tft.init(). Mode 0 leaves it blank: it latches on the wrong clock
        # edge, so every command byte arrives shifted. spi-cpol and spi-cpha
        # are the devicetree spelling of mode 3, and spi-esp32.c implements
        # both (SPI_CK_IDLE_EDGE for CPOL, SPI_CK_OUT_EDGE for CPHA).
        patch = PATCHES.joinpath(
            '0063-esp32s3-dts-nano-pin-map-st7789-panel-temp-node.patch').read_text()
        self.assertIn('spi-cpol;', patch)
        self.assertIn('spi-cpha;', patch)

    def test_the_node_claims_no_backlight_and_no_chip_select(self):
        # This module is broken out as SCL SDA RES DC plus power: there is no
        # BLK pad and no CS pad. The backlight is tied to VCC on the board, so
        # a led-gpios line would register a backlight device that controls
        # nothing; the CS is tied low, so the panel is selected permanently
        # and spi-esp32.c's own CS output is unconnected. Both properties are
        # therefore absent, and `reg = <0>` is what still tells the SPI core
        # which chip select to claim.
        patch = PATCHES.joinpath(
            '0063-esp32s3-dts-nano-pin-map-st7789-panel-temp-node.patch').read_text()
        self.assertNotIn('led-gpios   = <', patch)
        # The property form, not the bare string: the comment above says why
        # there is no cs-gpios, and that comment names it.
        self.assertNotIn('cs-gpios = <', patch)
        self.assertIn('reg = <0>;', patch)


class BootargsTests(unittest.TestCase):
    def test_a_patch_adds_console_tty1(self):
        found = [text for text in PATCHES.glob('*.patch')
                 if 'console=tty1' in text.read_text()]
        self.assertEqual(len(found), 1,
                         'exactly one patch should add console=tty1, got %r' % found)

    def test_the_serial_console_survives(self):
        # Both console= arguments have to be on the line. Dropping ttyS0 would
        # leave a board whose only keyboard is the serial port with no console
        # to type at.
        text = PATCHES.joinpath(
            '0067-esp32s3-the-panel-becomes-a-console.patch').read_text()
        added = [line for line in text.splitlines()
                 if line.startswith('+') and 'bootargs' in line]
        self.assertEqual(len(added), 1, added)
        self.assertIn('console=ttyS0,115200n8', added[0])
        self.assertIn('console=tty1', added[0])

    def test_the_serial_console_is_the_last_one_on_the_line(self):
        # `console` is a kernel alias for the LAST console= on the command
        # line, and /dev/console is routed to the same one. With tty1 last,
        # buildroot's `console::respawn:/sbin/getty -L console 0 vt100` --
        # which names the alias, not a device -- moved the serial getty onto
        # the panel, and the board came up with two gettys on tty1 and none on
        # ttyS0. The serial console took no input at all. The serial port is
        # this image's default console, so it has to be written last.
        text = PATCHES.joinpath(
            '0067-esp32s3-the-panel-becomes-a-console.patch').read_text()
        added = [line for line in text.splitlines()
                 if line.startswith('+') and 'bootargs' in line]
        self.assertEqual(len(added), 1, added)
        self.assertLess(added[0].index('console=tty1'),
                        added[0].index('console=ttyS0,115200n8'),
                        'ttyS0 has to come after tty1, or `console` and '
                        '/dev/console both mean the panel')

    def test_the_patch_series_is_numbered_without_gaps_or_duplicates(self):
        # buildroot applies the series in lexical order, so a number that is
        # already taken silently reorders the series.
        names = sorted(path.name for path in PATCHES.glob('*.patch'))
        numbers = [int(name[:4]) for name in names]
        self.assertEqual(numbers, sorted(set(numbers)), 'duplicate patch number')
        # 0048 was deleted with the BLE pipe and the gap is harmless: the
        # series is applied in lexical order, so 0047 -> 0049 needs no
        # renumbering.
        self.assertNotIn(48, numbers, 'the BLE-pipe patch is retired')


class InittabTests(unittest.TestCase):
    def test_trim_target_adds_a_getty_on_tty1(self):
        text = TRIM.read_text()
        self.assertIn("getty -L tty1", text,
                      'without a getty the panel shows kernel messages and no '
                      'login prompt')
        self.assertIn('::respawn:', text)

    def test_the_serial_getty_is_pinned_and_not_left_on_console(self):
        # `console` is a kernel alias for the LAST console= on the command
        # line. Patch 0067 appends console=tty1, so buildroot's
        # `console::respawn:/sbin/getty -L  console 0 vt100` stopped meaning
        # ttyS0 and moved onto the panel -- leaving the board with two gettys
        # on tty1 and none on the serial port. The serial console took no
        # input at all. This is that regression.
        text = TRIM.read_text()
        self.assertIn('ttyS0::respawn:/sbin/getty -L ttyS0', text,
                      'the serial getty has to name ttyS0, not the console alias')
        self.assertIn('^console::respawn:/sbin/getty', text,
                      'the console-alias line has to be rewritten, not appended to')

    def test_the_getty_line_is_added_once(self):
        # trim-target.sh runs over a tree that may already carry the line, and
        # a second getty on the same terminal fights the first for it. The
        # guard is a delete-then-append, not an append-if-absent: the earlier
        # `grep -q ... || echo ... >>` form was idempotent against itself and
        # helpless against a line that differed, so an older build's
        # `::respawn:/sbin/getty -L tty1 0 vt100` sat in target/ and the
        # `linux` this script writes was never appended. The board shipped
        # vt100 on the panel for two builds while this file said linux. What
        # has to be true is therefore that the line is deleted before it is
        # written, so the value in the script is the value that ships.
        text = TRIM.read_text()
        self.assertIn("sed -i '/^::respawn:\\/sbin\\/getty -L tty1 /d'", text,
                      'the tty1 line has to be deleted before it is appended, '
                      'or a stale line from an earlier build survives')
        self.assertIn('::respawn:/sbin/getty -L tty1 0 linux', text,
                      'the panel getty has to name the `linux` terminal type; '
                      'vt100 has no colour and tells the shell the wrong width')

    def test_the_line_survives_the_sed_above_it(self):
        # The swapon/swapoff/ttyGS3 deletions run first. A line matching one of
        # those patterns would be deleted on the way in.
        for pattern in ('swapon', 'swapoff', 'ttyGS3'):
            with self.subTest(pattern=pattern):
                self.assertNotIn(pattern, '::respawn:/sbin/getty -L tty1 0 vt100')


class PartitionTests(unittest.TestCase):
    def test_factory_layout_leaves_four_mib_after_rootfs(self):
        parts = dict((name, (offset, size)) for name, offset, size in partitions(CSV.read_text()))
        self.assertEqual(parts['linux'], (0x140000, 0x3d0000))
        self.assertEqual(parts['rootfs'], (0x510000, 0x6f0000))
        self.assertEqual(sum(parts['rootfs']), 0xc00000)
        self.assertEqual(parts['factory'], (0x10000, 0xc0000))
        self.assertEqual(set(parts), {'factory', 'linux', 'rootfs'})

    def test_the_linux_offset_did_not_move(self):
        # CONFIG_KERNEL_LOAD_ADDRESS is 0x42000000 + the linux offset, and
        # package-final.py asserts the link address against it. Moving the
        # offset without moving the load address boot-loops the board.
        parts = dict((name, (offset, size)) for name, offset, size in partitions(CSV.read_text()))
        self.assertEqual(parts['linux'][0], 0x00140000)
        config = settings(CONFIG.read_text())
        self.assertEqual(config.get('CONFIG_KERNEL_LOAD_ADDRESS'), '0x42140000')

    def test_factory_partitions_do_not_overlap(self):
        rows = partitions(CSV.read_text())
        end = 0x10000
        for name, offset, size in rows:
            with self.subTest(partition=name):
                self.assertGreaterEqual(offset, end)
            end = offset + size
        self.assertEqual(end, 0xc00000)

    def test_kernel_partitions_match_factory_csv(self):
        patch = next(CSV.parents[2].glob('board/espressif/esp32s3/patches/linux/0074-*'))
        fields = re.findall(r'label = "([^"]+)";\n\+\s+reg = <(0x[0-9a-f]+) (0x[0-9a-f]+)>;',
                            patch.read_text())
        kernel = {name: (int(offset, 0), int(size, 0)) for name, offset, size in fields}
        csv = {name: (offset, size) for name, offset, size in partitions(CSV.read_text())}
        self.assertEqual(kernel, csv)


class PackagingTests(unittest.TestCase):
    def test_the_build_asserts_every_image_fits(self):
        text = PACKAGE.read_text()
        self.assertIn("parts[partition]['size']", text,
                      'package-final.py has to refuse to package an overflowing image')

    def test_the_build_asserts_the_load_address_matches_the_offset(self):
        text = PACKAGE.read_text()
        self.assertIn('0x42000000 + parts', text,
                      'the link address and the partition offset are one decision')


if __name__ == '__main__':
    unittest.main(verbosity=2)
