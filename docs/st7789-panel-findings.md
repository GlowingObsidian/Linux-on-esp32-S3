# The 240x240 ST7789 panel: what the kernel already has, and the four traps

Research notes for moving this image's panel from the 128x160 ST7735R to a
240x240 ST7789. Everything below was read out of the pinned 7.2.4 tree in the
build container, not from memory.

Kernel source root in the container:

```
/refs/esp32-linux-build/build/build-buildroot-arduino_nano_esp32/build/linux-7.2.4
```

## The driver is already there

`drivers/staging/fbtft/fb_st7789v.c` registers `"sitronix,st7789v"` and its
display struct is:

```c
static struct fbtft_display display = {
	.regwidth = 8,
	.width = 240,
	.height = 320,
	.gamma_num = 2,
	.gamma_len = 14,
	.gamma = HSD20_IPS_GAMMA,
	.fbtftops = { ... },
};

FBTFT_REGISTER_DRIVER(DRVNAME, "sitronix,st7789v", &display);
```

`CONFIG_FB_TFT_ST7789V` is the Kconfig symbol
(`drivers/staging/fbtft/Kconfig:146`).

## Trap 1 — the driver says 320, the panel is 240

`.height = 320` is the controller's column count, not the module's. The module
is a 240x240 window onto a 240x320 controller.

fbtft takes the geometry from the driver and only lets the devicetree override
it — `fbtft-core.c:543-546`:

```c
	if (pdata->display.width)
		display->width = pdata->display.width;
	if (pdata->display.height)
		display->height = pdata->display.height;
```

and the property names are **bare**, `fbtft-core.c:1101-1102`:

```c
	pdata->display.width = fbtft_property_value(dev, "width");
	pdata->display.height = fbtft_property_value(dev, "height");
```

`fbtft_property_value()` is `device_property_read_u32(dev, propname, &val)`
(`fbtft-core.c:1076-1086`) with no vendor prefix anywhere. So the node carries
`width = <240>` and `height = <240>`, and a `fbtft,width` spelling would be read
as absent and ignored without a warning — leaving a framebuffer 80 rows too
tall.

## Trap 2 — the address window has no offset, and this panel needs one

This is the one trap that is not settled by the devicetree, and it is the
likeliest thing to go wrong on the first boot.

**fbtft cannot express an offset at all.** `fb_st7789v.c` defines **no**
`set_addr_win` of its own, so it inherits `fbtft_set_addr_win`
(`fbtft-core.c:200-210`):

```c
static void fbtft_set_addr_win(struct fbtft_par *par, int xs, int ys, int xe,
			       int ye)
{
	write_reg(par, MIPI_DCS_SET_COLUMN_ADDRESS,
		  (xs >> 8) & 0xFF, xs & 0xFF, (xe >> 8) & 0xFF, xe & 0xFF);

	write_reg(par, MIPI_DCS_SET_PAGE_ADDRESS,
		  (ys >> 8) & 0xFF, ys & 0xFF, (ye >> 8) & 0xFF, ye & 0xFF);

	write_reg(par, MIPI_DCS_WRITE_MEMORY_START);
}
```

and `fbtft_update_display()` calls it as
`set_addr_win(par, 0, start_line, xres - 1, end_line)`
(`fbtft-core.c:265-266`). **Column 0, row 0.** There is no `colstart`/`rowstart`
anywhere in `drivers/staging/fbtft/`, and the only geometry properties
`fbtft_properties_read()` parses are `width`, `height`, `regwidth`, `buswidth`,
`bpp` and `rotate` (`fbtft-core.c:1101-1112`). The binding
(`Documentation/devicetree/bindings/display/panel/sitronix,st7789v.yaml`)
defines no offset property either.

**The Adafruit library says the offset is 80, not 0.** `Adafruit_ST7789::init()`
(`Adafruit_ST7789.cpp:91-129`, current master):

```cpp
if (width == 240 && height == 240) {
    // 1.3", 1.54" displays (right justified)
    _rowstart = (320 - height);          // = 80
    _rowstart2 = 0;
    _colstart = _colstart2 = (240 - width);  // = 0
}
```

and `setAddrWindow()` (`Adafruit_ST77xx.cpp:158-172`) adds `_xstart`/`_ystart`
to every blit, so a `setAddrWindow(0, 0, 240, 240)` on a 240x240 module emits
CASET 0..239 and **RASET 80..319**. The same 0/80 pair is baked into the older
v1.3.0 `cmd_240x240` table's RASET. `russhughes/st7789_mpy`'s default
orientation table agrees for rotation 0 and 1 (`y_start` 0) and uses 80 for
rotations 2 and 3.

**Why:** the controller's GRAM is 240x320. A 240x240 module wires only 240 of
the 320 rows to glass, and which 240 is a property of the module's own
wiring — the dominant convention (Adafruit, TFT_eSPI's `CGRAM_OFFSET`,
st7789_mpy) is bottom-aligned, visible rows GRAM 80..319. A top-aligned module
would want offset 0. **Nothing in the devicetree, and nothing in fbtft, can
say which this module is.**

**What mainline does instead.** `drivers/gpu/drm/panel/panel-sitronix-st7789v.c`
has a 240x280 panel, `jt240mhqs_hwt_ek_e3_panel`, and handles the 40 missing
rows with the controller's partial-display window rather than an address
offset:

```c
	.partial_mode = true,
	.partial_start = 38,
	.partial_end = 318,
```

`st7789v_prepare()` then emits `MIPI_DCS_ENTER_PARTIAL_MODE`,
`MIPI_DCS_SET_PAGE_ADDRESS` and `MIPI_DCS_SET_PARTIAL_ROWS`. That is a
different mechanism from Adafruit's — it makes the controller scan out only
those rows — and fbtft has no equivalent. It is also a DRM driver, so it is not
usable here at all.

**Consequence.** If the panel comes up with the image shifted 80 rows, or with
80 rows of the frame off the glass, the fix is a driver change: a
`set_addr_win` override in `fb_st7789v.c` that adds a row offset, or an
init-sequence `MIPI_DCS_SET_PAGE_ADDRESS` / partial-mode write, with the offset
as a devicetree property. It is not a config change and it is not a node
property. This is the first thing to check on the board.

### Settled on the board: this module is top-aligned, offset 0

The first boot with `row-offset = <80>` showed the symptom the paragraph above
predicts — boot logs on the glass, but the top third of it blank. 80 of 240 is
exactly one third. `echo 0 > /sys/class/graphics/fb0/row_offset` (the knob
patch 0069 adds) moved the image flush to the top edge, and the bottom text
line is unclipped. So **this module is top-aligned, visible GRAM rows 0..239**,
and Adafruit's `_rowstart = 320 - height` does not hold for it.

That is the useful correction to this document: 80 is the *dominant convention*,
not a constant, and the convention was wrong for the one module in hand. Which
is the whole reason the offset is a devicetree property with a sysfs knob
beside it rather than a number compiled into the driver.

**The offset and MADCTL are coupled, and both are now settled.** Adafruit's
working sketch sends `_rowstart = 80` *and* MADCTL `0xC0` (MX|MY) together —
those two are consistent with each other, because flipping the address counters
reaches the same physical rows from the other end of the range. fbtft sends
`0x00` at rotation 0, so the pair that is consistent with *it* is offset 0.
The board confirms this: offset 0 with MADCTL 0 gives a correct, flush-to-top
image. Both pairs describe the same physical panel. The practical consequence
is that if MADCTL is ever changed to `0xC0` — which is what a 180-degree look
needs, and what `echo 192 > /sys/class/graphics/fb0/madctl` does live — the
offset may need to go back to 80. Change one, then re-check the top edge.

## Trap 3 — SPI mode 3, and the driver never sets a mode

`fb_st7789v.c` sets no SPI mode. The SPI core leaves the device at mode 0
unless the devicetree says otherwise, and mode 0 leaves this panel blank: it
latches on the wrong clock edge, so every command byte arrives shifted.

The devicetree spelling of mode 3 is `spi-cpol` (CPOL=1) with `spi-cpha`
(CPHA=1). `spi-esp32.c` implements all four modes:

- `spi-esp32.c:155` — `if (device->mode & SPI_CPOL) cr |= SPI_CK_IDLE_EDGE;`
- `spi-esp32.c:232-233` — `SPI_CK_OUT_EDGE` for CPOL^CPHA
- `spi-esp32.c:389` — `host->mode_bits = SPI_CPHA | SPI_CPOL | ...`

This is the mode the Adafruit sketch that brought the panel up passes to
`tft.init()`.

## Trap 4 — the driver forces colour inversion

`fb_st7789v.c:47` defines `HSD20_IPS 1`, and `init_display()` ends
(`fb_st7789v.c:214-215`):

```c
	if (HSD20_IPS)
		write_reg(par, MIPI_DCS_ENTER_INVERT_MODE);
```

Unconditional, with no devicetree property to turn it off. The ST7735R driver
does the opposite — `fb_st7735r.c:72` sends `MIPI_DCS_EXIT_INVERT_MODE`.

This is correct for the HSD20 IPS glass the driver was written for, and it is
what the Adafruit library does for most ST7789 modules too. **If the panel comes
up with inverted colours** — black where white should be — this is the line,
and there is no property to change it. The fix would be a small patch to
`fb_st7789v.c` making `HSD20_IPS` a module parameter or a devicetree boolean.

## The other driver in the tree

`drivers/gpu/drm/panel/panel-sitronix-st7789v.c` registers the same
`"sitronix,st7789v"` compatible (line 675). It is a **DRM** panel driver: it
needs a DRM/KMS display controller and a parallel RGB or DSI bus, and this
board has neither. It cannot be used here — the fbtft driver is the only one
that fits an SPI framebuffer. Worth knowing only because it is the source of
the 240x240-on-240x320 evidence above.

## What the panel node needs

```dts
panel@0 {
	compatible = "sitronix,st7789v";
	reg = <0>;
	spi-max-frequency = <20000000>;
	spi-cpol;
	spi-cpha;
	width  = <240>;
	height = <240>;
	row-offset = <0>;
	buswidth = <8>;
	dc-gpios    = <&gpio0 6 GPIO_ACTIVE_HIGH>;	/* D3 */
	reset-gpios = <&gpio0 7 GPIO_ACTIVE_LOW>;	/* D4 */
};
```

`buswidth = <8>` is required for the same reason it was on the ST7735R:
`fbtft_probe_common()` rejects the device with "buswidth is not set" when
`display->buswidth` is 0, and `fb_st7789v.c` sets no default.

`row-offset = <0>` is the board's answer, written out rather than left off
because the value is a finding about this module. It is also the value with no
property at all — `fbtft_framebuffer_alloc()` only copies
`pdata->display.row_offset` when it is non-zero — so the line is documentation
as much as configuration. `col-offset` and `madctl` are parsed the same way and
are absent here: zero is right for both, and neither has a module-specific
value to record.

## Pins: no backlight pad, no chip select pad

This module is broken out as `SCL SDA RES DC` plus power. Neither property is
on the node, and both absences are deliberate.

- **No backlight pin.** The backlight is tied to VCC on the module, so it is on
  whenever the module is powered. `led-gpios` would have nothing real to drive:
  `fbtft_properties_read()` sets `display.backlight` when the property is
  present (`fbtft-core.c:1115-1117`) and the driver registers a backlight
  device on that GPIO, so a line pointed at a spare header pin would give
  userspace a `/sys/class/backlight` entry that controls nothing. The property
  is left out and no backlight device is registered. D2 stays free.
- **No chip select pad.** `spi-esp32.c` drives CS itself through `SPI_MISC_REG`
  and sets `host->set_cs` (`esp32_spi_set_cs`), and the four SPI2 pads are
  routed through the GPIO matrix with `FSPICS0_OUT` on D10 — see patch 0063. A
  module with no CS pad is a module whose CS is tied low on the board, so it is
  selected permanently and the controller's CS output is simply not connected.
  That works: it is the only device on the bus. `reg = <0>` still has to be
  there, because that is what tells the SPI core which chip select to claim,
  whether or not the pad is wired.
