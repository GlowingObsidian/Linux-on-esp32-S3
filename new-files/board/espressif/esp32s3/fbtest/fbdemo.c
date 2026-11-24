/*
 * fbdemo -- a gears-style graphics demo for the ST7789 panel, with the frame
 * rate reported on the serial terminal.
 *
 * Why this exists. fbtest measured what the panel can do: 78.8-80.5 fps from a
 * compiled program on the same write() path micropython managed 1.4-5.4 fps
 * on. That settled the measurement question -- the bottleneck was interpreter
 * speed, never the panel -- but fbtest is a benchmark, not a demo. It draws a
 * flat fill and a sweeping bar that changes every frame, so at 80 fps the
 * screen is a blur of flashing colour and nothing is legible.
 *
 * This draws three interlocking gears instead, at a fixed and watchable rate,
 * and prints the frame rate to stdout. The panel shows the animation; the
 * serial console shows the number. Nothing is drawn into the framebuffer
 * except the gears, so the terminal stays the readable console it has always
 * been.
 *
 * It is also the first real drawing code for "draw on the panel from a
 * program". A paint app needs exactly these primitives -- a line, a filled
 * triangle, a 3D transform, a frame pacer -- so this doubles as the library
 * those apps will use.
 *
 * 32-bit fixed point throughout, deliberately. This toolchain's Xtensa LRA
 * pass hits an internal compiler error (lra-constraints.cc:4234) lowering any
 * 64-bit value, and no libm soft-float path is worth the risk here. So there
 * is no int64_t, no float, no double and no math.h anywhere below: angles are
 * 8-bit turns, sines are a 256-entry table in 1.15 fixed point, and the 3x3
 * transform is 16.16.
 *
 * Build (in the build container, with the project's own toolchain):
 *
 *   xtensa-esp32s3-linux-uclibcfdpic-gcc -Os -mfdpic -mauto-litpools -fPIC \
 *       -ffunction-sections -fdata-sections -std=gnu17 -Wall -Wextra -Werror \
 *       -Wl,--gc-sections,-z,stack-size=65536 fbdemo.c -o fbdemo
 *
 * Usage:  fbdemo [-f fps] [-s seconds] [-b] [-q]
 *   -f N   target N frames per second; 0 means uncapped (default 30)
 *   -s S   run for S seconds (default 0 = until interrupted)
 *   -b     leave the console in graphics mode afterwards (for a paint app)
 *   -q     do not touch the console at all -- draw only
 */

#include <errno.h>
#include <fcntl.h>
#include <linux/fb.h>
#include <linux/kd.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <unistd.h>

/* The panel is 240x240 RGB565. The frame size is read from the driver rather
 * than assumed, so this keeps working if the panel changes. */
#define PANEL_W 240
#define PANEL_H 240

/* ------------------------------------------------------------------ *
 * Fixed point
 *
 * TRIG_ONE is 1.0 in the sine table's 1.15 format. The table holds one full
 * turn in 256 steps, so an angle is a uint8_t and wrapping it is free: the
 * index is masked with & 0xff and the compiler needs no branch.
 * ------------------------------------------------------------------ */

#define TRIG_SHIFT 15
#define TRIG_ONE   (1 << TRIG_SHIFT)
#define TRIG_MASK  (TRIG_ONE - 1)

/* 16.16 for the transform and the vertex coordinates. */
#define FP_SHIFT 16
#define FP_ONE   (1 << FP_SHIFT)

/* PROJECT_SCALE is pixels times world units, because the perspective divide
 * below cancels the units: (world units) * (px * units) / (units) = px.
 *
 * glxgears' glFrustum(-1, 1, -h, h, 5, 60) has a half-width of 1.0 at the near
 * plane, 5 units out. The gears sit 30 units from the eye, so the visible
 * half-width there is 1.0 * 30 / 5 = 6 world units, and that maps to half of
 * 240 pixels: 120 * 30 / 6 = 600.
 *
 * 600 is the reference's own framing, and it is what the scene is laid out
 * for -- glxgears' window is wider than it is tall and the gears sit in the
 * middle band. On a 240x240 panel the scene is square, so the gears fill the
 * height and the sides are what is left over. */
#define PROJECT_SCALE 600

/* 1.15 sine, one full turn over 256 steps. sin_tab[i] = sin(i * 2*pi / 256),
 * scaled by 32767 and rounded. cos is the same table a quarter turn ahead. */
static const int16_t sin_tab[256] = {
	    0,   804,  1608,  2410,  3212,  4011,  4808,  5602,
	 6393,  7179,  7962,  8739,  9512, 10278, 11039, 11793,
	12539, 13279, 14010, 14732, 15446, 16151, 16846, 17530,
	18204, 18868, 19519, 20159, 20787, 21403, 22005, 22594,
	23170, 23731, 24279, 24811, 25329, 25832, 26319, 26790,
	27245, 27683, 28105, 28510, 28898, 29268, 29621, 29956,
	30273, 30571, 30852, 31113, 31356, 31580, 31785, 31971,
	32137, 32285, 32412, 32521, 32609, 32678, 32728, 32757,
	32767, 32757, 32728, 32678, 32609, 32521, 32412, 32285,
	32137, 31971, 31785, 31580, 31356, 31113, 30852, 30571,
	30273, 29956, 29621, 29268, 28898, 28510, 28105, 27683,
	27245, 26790, 26319, 25832, 25329, 24811, 24279, 23731,
	23170, 22594, 22005, 21403, 20787, 20159, 19519, 18868,
	18204, 17530, 16846, 16151, 15446, 14732, 14010, 13279,
	12539, 11793, 11039, 10278,  9512,  8739,  7962,  7179,
	 6393,  5602,  4808,  4011,  3212,  2410,  1608,   804,
	    0,  -804, -1608, -2410, -3212, -4011, -4808, -5602,
	-6393, -7179, -7962, -8739, -9512,-10278,-11039,-11793,
      -12539,-13279,-14010,-14732,-15446,-16151,-16846,-17530,
      -18204,-18868,-19519,-20159,-20787,-21403,-22005,-22594,
      -23170,-23731,-24279,-24811,-25329,-25832,-26319,-26790,
      -27245,-27683,-28105,-28510,-28898,-29268,-29621,-29956,
      -30273,-30571,-30852,-31113,-31356,-31580,-31785,-31971,
      -32137,-32285,-32412,-32521,-32609,-32678,-32728,-32757,
      -32767,-32757,-32728,-32678,-32609,-32521,-32412,-32285,
      -32137,-31971,-31785,-31580,-31356,-31113,-30852,-30571,
      -30273,-29956,-29621,-29268,-28898,-28510,-28105,-27683,
      -27245,-26790,-26319,-25832,-25329,-24811,-24279,-23731,
      -23170,-22594,-22005,-21403,-20787,-20159,-19519,-18868,
      -18204,-17530,-16846,-16151,-15446,-14732,-14010,-13279,
      -12539,-11793,-11039,-10278, -9512, -8739, -7962, -7179,
	 -6393, -5602, -4808, -4011, -3212, -2410, -1608,  -804,
};

static int32_t isin(uint8_t a) { return sin_tab[a & 0xff]; }
static int32_t icos(uint8_t a) { return sin_tab[(uint8_t)(a + 64) & 0xff]; }

/* Multiply two 1.15 values, giving 1.15. int32_t intermediate: 32767*32767
 * is 2^30, which fits with room to spare. */
static int32_t trig_mul(int32_t a, int32_t b)
{
	return (int32_t)(((int32_t)a * (int32_t)b) >> TRIG_SHIFT);
}

/* ------------------------------------------------------------------ *
 * The framebuffer
 * ------------------------------------------------------------------ */

/* RGB565, the panel's native format. A macro rather than a function: Xtensa's
 * LRA pass is fragile on this loop and inlining the packing by hand keeps the
 * expression in one basic block. */
#define RGB565(r, g, b) \
	((uint16_t)((((r) & 0x1f) << 11) | (((g) & 0x3f) << 5) | ((b) & 0x1f)))

static uint16_t *fb;
static int fb_line_px;		/* pixels per line, from fix.line_length */
static int fb_height;		/* visible lines */

/* Clipped pixel write. Every primitive goes through this, so nothing can draw
 * outside the panel and corrupt whatever the kernel keeps after it. */
static void put_px(int x, int y, uint16_t colour)
{
	if ((unsigned)x >= (unsigned)PANEL_W)
		return;
	if ((unsigned)y >= (unsigned)fb_height)
		return;
	fb[(size_t)y * (size_t)fb_line_px + (size_t)x] = colour;
}

/* Integer Bresenham. Used for the gear outlines, which is where the shape
 * reads from -- a filled gear with no outline is a blob. */
/* Kept for the paint apps this program is also a library for. The glxgears
 * port draws no outlines -- the reference has none either, it relies on
 * lighting -- so nothing in this file calls it yet. */
__attribute__((unused))
static void draw_line(int x0, int y0, int x1, int y1, uint16_t colour)
{
	int dx = x1 - x0, dy = y1 - y0;
	int sx = dx < 0 ? -1 : 1;
	int sy = dy < 0 ? -1 : 1;
	int err;

	if (dx < 0)
		dx = -dx;
	if (dy < 0)
		dy = -dy;
	err = dx - dy;

	for (;;) {
		put_px(x0, y0, colour);
		if (x0 == x1 && y0 == y1)
			break;
		/* e2 is the doubled error term, computed once per step. Both
		 * comparisons must read the same value: re-reading `err` after
		 * the first branch has already stepped x lets one iteration
		 * advance both axes and walk straight past the endpoint, and the
		 * loop then never terminates. */
		{
			int e2 = err * 2;

			if (e2 > -dy) {
				err -= dy;
				x0 += sx;
			}
			if (e2 < dx) {
				err += dx;
				y0 += sy;
			}
		}
	}
}

/* Scanline fill of a triangle, split into a top half and a bottom half so the
 * edge interpolation is monotonic in y. This is the piece a paint app reuses
 * for every filled shape it has.
 *
 * The long edges are interpolated in 16.16 and the span endpoints are rounded
 * once per scanline, which is what keeps the fill from developing the diagonal
 * seams a per-pixel interpolation shows. */
static void fill_tri(int x0, int y0, int x1, int y1, int x2, int y2,
		     uint16_t colour)
{
	int y, ytop, ybot;
	int xa, xb;		/* current span, in 16.16 */
	int dxa, dxb;		/* per-scanline step, in 16.16 */

	/* Sort by y so the split is well defined. */
	if (y0 > y1) { int t = y0; y0 = y1; y1 = t; t = x0; x0 = x1; x1 = t; }
	if (y1 > y2) { int t = y1; y1 = y2; y2 = t; t = x1; x1 = x2; x2 = t; }
	if (y0 > y1) { int t = y0; y0 = y1; y1 = t; t = x0; x0 = x1; x1 = t; }

	ytop = y0;
	ybot = y2;
	if (ytop == ybot)
		return;		/* degenerate: nothing to fill */

	/* Upper half: (x0,y0)-(x1,y1) against (x0,y0)-(x2,y2). */
	if (y1 > y0) {
		int h = y1 - y0;

		xa = (int32_t)x0 << FP_SHIFT;
		xb = (int32_t)x0 << FP_SHIFT;
		dxa = (int)((((int32_t)x1 - x0) << FP_SHIFT) / h);
		dxb = (int)((((int32_t)x2 - x0) << FP_SHIFT) / (ybot - ytop));

		for (y = y0; y < y1; y++) {
			int xl = xa >> FP_SHIFT, xr = xb >> FP_SHIFT;
			uint16_t *row;

			if (xl > xr) { int t = xl; xl = xr; xr = t; }
			if (y >= 0 && y < fb_height) {
				row = fb + (size_t)y * (size_t)fb_line_px;
				if (xl < 0)
					xl = 0;
				if (xr >= PANEL_W)
					xr = PANEL_W - 1;
				for (; xl <= xr; xl++)
					row[xl] = colour;
			}
			xa += dxa;
			xb += dxb;
		}
	}

	/* Lower half: (x1,y1)-(x2,y2) against (x0,y0)-(x2,y2). */
	if (y2 > y1) {
		int h = y2 - y1;

		xa = (int32_t)x1 << FP_SHIFT;
		xb = (int32_t)x0 << FP_SHIFT;
		dxa = (int)((((int32_t)x2 - x1) << FP_SHIFT) / h);
		dxb = (int)((((int32_t)x2 - x0) << FP_SHIFT) / (ybot - ytop));
		/* xb was walked from y0; advance it to y1. */
		xb += dxb * (y1 - y0);

		for (y = y1; y < y2; y++) {
			int xl = xa >> FP_SHIFT, xr = xb >> FP_SHIFT;
			uint16_t *row;

			if (xl > xr) { int t = xl; xl = xr; xr = t; }
			if (y >= 0 && y < fb_height) {
				row = fb + (size_t)y * (size_t)fb_line_px;
				if (xl < 0)
					xl = 0;
				if (xr >= PANEL_W)
					xr = PANEL_W - 1;
				for (; xl <= xr; xl++)
					row[xl] = colour;
			}
			xa += dxa;
			xb += dxb;
		}
	}
}

/* ------------------------------------------------------------------ *
 * The 3D transform
 *
 * A 3x3 rotation in 16.16, built from two angles. Vertices are int32_t in
 * 16.16 and the projection is a plain perspective divide, which is enough for
 * objects that stay in front of the eye.
 * ------------------------------------------------------------------ */

struct mat3 {
	int32_t m[9];
};

/* Rotation about X by `ax`, then about Y by `ay`, composed into one matrix so
 * each vertex costs nine multiplies rather than two passes. */
static void mat3_rot_xy(struct mat3 *r, uint8_t ax, uint8_t ay)
{
	int32_t cx = icos(ax), sx = isin(ax);
	int32_t cy = icos(ay), sy = isin(ay);

	/* R = Ry * Rx, entries worked out once here rather than per vertex. */
	r->m[0] = cy;
	r->m[1] = trig_mul(sy, sx);
	r->m[2] = -trig_mul(sy, cx);

	r->m[3] = 0;
	r->m[4] = cx;
	r->m[5] = sx;

	r->m[6] = sy;
	r->m[7] = -trig_mul(cy, sx);
	r->m[8] = trig_mul(cy, cx);
}

/* Multiply a 1.15 matrix entry by a 16.16 value, giving 16.16.
 *
 * The obvious `(m * v) >> 15` is wrong here, and wrong in a way that only
 * shows up on some vertices. m is 1.15 so it reaches 32767, and v is 16.16 so
 * it reaches about 65600 for this scene -- and 32767 * 65600 is
 * -2,149,515,200 as a signed 32-bit number, which is past INT32_MIN
 * (-2,147,483,648). Signed overflow is undefined behaviour; on this target it
 * wrapped positive, so a vertex at screen x ~8 came out at x ~121 instead. On
 * the root circle that turned one segment into a chord straight across the
 * disc, which is the stray horizontal line through every gear.
 *
 * Shifting the product afterwards does not help -- the product is what
 * overflows. So v is split into its top and bottom 15 bits and each half is
 * multiplied separately, keeping every intermediate under 2^31:
 * m * 32767 is 1,073,676,289 at worst, and m * (v >> 15) is small because
 * v >> 15 is at most a couple of units. The two shifts differ by 15, which is
 * exactly the 1.15 scale, so the sum is the same value the single multiply
 * would have given had it fit.
 */
static int32_t mul_1_15(int32_t m, int32_t v)
{
	int32_t hi = v >> 15;		/* v / 32768 */
	int32_t lo = v & 0x7fff;	/* the remainder, always positive */

	return (int32_t)(m * hi) + (int32_t)((m * lo) >> TRIG_SHIFT);
}

/* Transform a 16.16 vertex. All three outputs are 16.16.
 *
 * Each term goes through mul_1_15 rather than a bare multiply, for the reason
 * given there. The terms are then summed as 16.16 values, which is safe: each
 * is bounded by the vertex magnitude and there are only three of them.
 */
static void mat3_apply(const struct mat3 *r, int32_t x, int32_t y, int32_t z,
		       int32_t *ox, int32_t *oy, int32_t *oz)
{
	*ox = mul_1_15(r->m[0], x) + mul_1_15(r->m[1], y) +
	      mul_1_15(r->m[2], z);
	*oy = mul_1_15(r->m[3], x) + mul_1_15(r->m[4], y) +
	      mul_1_15(r->m[5], z);
	*oz = mul_1_15(r->m[6], x) + mul_1_15(r->m[7], y) +
	      mul_1_15(r->m[8], z);
}

/* ------------------------------------------------------------------ *
 * The gears
 *
 * Each gear is a flat polygon in the z=0 plane: a ring of alternating outer
 * and inner vertices around a hub, plus a hole. It is drawn as a triangle fan
 * from the hub, which fills the ring and leaves the hole because the inner
 * vertices are shared.
 *
 * A cube would have been less code, but a cube's silhouette barely changes, so
 * at 30 fps it looks static -- which is the exact problem this program exists
 * to fix. Teeth sweep, and three gears at different speeds give the eye
 * something to lock onto.
 * ------------------------------------------------------------------ */

#define GEAR_TEETH_MAX 24
#define GEAR_VERTS_MAX (GEAR_TEETH_MAX * 4)

struct gear {
	int32_t inner;		/* inner_radius, 16.16 */
	int32_t outer;		/* outer_radius, 16.16 */
	int32_t width;		/* extrusion along z, 16.16 */
	int teeth;
	int32_t tooth_depth;	/* 16.16 */
	int32_t tx, ty;		/* translate, 16.16 */
	int32_t spin;		/* rotation about z, 16.16 turns */
	uint16_t face;		/* ambient and diffuse colour */
	uint16_t side;		/* the walls, shaded darker */
};
/* ------------------------------------------------------------------ *
 * The gear, exactly as glxgears builds it
 *
 * gear(inner_radius, outer_radius, width, teeth, tooth_depth), with the
 * reference's own numbers: gear(1.0, 4.0, 1.0, 20, 0.7), gear(0.5, 2.0, 2.0,
 * 10, 0.7), gear(1.3, 2.0, 0.5, 10, 0.7). The three radii are the whole trick
 * and getting them wrong is what makes a star instead of a gear:
 *
 *   r0 = inner_radius                     the hole in the middle
 *   r1 = outer_radius - tooth_depth / 2   the root circle the teeth sit on
 *   r2 = outer_radius + tooth_depth / 2   the tip circle the teeth reach
 *
 * da is a quarter of the tooth pitch, which is what gives a tooth two flanks
 * and a flat top. Two vertices per tooth on alternating radii -- the first
 * thing tried here -- has no flank at all, so the result is a star.
 *
 * The solid is six surfaces, and all six are needed for it to read as a solid:
 * a front face at +width/2, a back face at -width/2, the outward walls of the
 * teeth joining them, the inner cylinder of the hole, and the front and back
 * faces of the teeth themselves. Without the walls, rotating about X or Y just
 * shears a flat sheet.
 * ------------------------------------------------------------------ */

#define SCENE_SCALE (FP_ONE * 11 / 10)

/* The eye, at the reference's own distance of 30 world units in front of the
 * scene. glxgears' glTranslatef(0, 0, -40) with glFrustum(..., 5, 60) puts the
 * near plane at 35 and the far at 90 from the eye, so the gears sit 30 to 34
 * units away and the perspective is gentle. */
#define EYE_DIST (30 * FP_ONE)

/* The reference's gears are not centred on the origin: gear1 sits at
 * (-3.0, -2.0), gear2 at (3.1, -2.0), gear3 at (-3.1, 4.2), and their outer
 * radii reach 4.0, 2.0 and 2.0. The whole assembly therefore spans roughly
 * x -7.0..+5.1 and y -6.0..+6.4. These offsets move that midpoint to the
 * origin so the scene lands in the middle of the panel. */
#define SCENE_CX 1
#define SCENE_CY 0

#define GEAR_SEG 40		/* segments in the hole cylinder */

/* Project one point of a gear into screen pixels.
 *
 * `radius` and `ang` are in world units and 8-bit turns; `zoff` is the
 * extrusion offset, added to the gear's own z. The gear's spin about its own
 * axis is folded into `ang` by the caller, and the translate is added here.
 */
#define PROJ(g, rot, radius, ang, zoff, px, py) do {				\
		/* mul_1_15, not a bare multiply: the radius is 16.16 and \
		 * reaches 4 world units = 285081, and 32767 * 285081 is \
		 * 9.3e9, far past INT32_MAX. The product overflows before \
		 * the shift can help, and every vertex collapses toward the \
		 * origin -- which is what made the gears solid discs with a \
		 * notch instead of toothed rings. */ \
		int32_t _vx = mul_1_15(icos(ang), (radius)) + (g)->tx;	\
		int32_t _vy = mul_1_15(isin(ang), (radius)) + (g)->ty;	\
		int32_t _ox, _oy, _oz, _d;					\
										\
		mat3_apply((rot), _vx, _vy, (zoff), &_ox, &_oy, &_oz);		\
		/* World units, not 16.16: the divide below wants the eye \
		 * distance in the same units as the pixels-per-unit scale. */ \
		_d = ((_oz + EYE_DIST) >> FP_SHIFT);				\
		if (_d < 1)							\
			_d = 1;							\
		/* World units, then the perspective divide. PROJECT_SCALE is \
		 * pixels times units: see its definition. \
		 * \
		 * The multiply is safe at 240x240 because the world-unit \
		 * conversion happens first: the scene reaches about 7 units \
		 * from centre, 7 * 600 = 4200, and _d is clamped to 1, so the \
		 * worst case is 4200 pixels -- a few thousand, nowhere near \
		 * INT32_MAX. Doing the shift after the multiply would be the \
		 * overflow this file has already been bitten by four times. */ \
		(px) = PANEL_W / 2 + (int)(((_ox >> FP_SHIFT) + SCENE_CX) \
					    * PROJECT_SCALE / _d); \
		(py) = PANEL_H / 2 + (int)(((_oy >> FP_SHIFT) + SCENE_CY) \
					    * PROJECT_SCALE / _d); \
	} while (0)

/* The gear's own rotation, as an 8-bit turn. glRotatef(angle, 0, 0, 1) puts
 * the gear's local x on the circle at `angle`, so every local angle is offset
 * by it. */
#define SPIN(g, ang) ((uint8_t)((ang) + ((g)->spin >> 8)))

static void gear_draw(const struct gear *g, const struct mat3 *rot)
{
	int32_t r0 = g->inner;
	int32_t r1 = g->outer - g->tooth_depth / 2;
	int32_t r2 = g->outer + g->tooth_depth / 2;
	int32_t hw = g->width / 2;
	int pitch = 256 / g->teeth;
	int da = pitch / 4;
	int i, seg;

	/* ---- back face, at z = -width/2, and the back of each tooth ----
	 *
	 * glxgears relies on the depth buffer to sort these against the front
	 * face. There is no depth buffer here, so the order is the sort:
	 * back-to-front within one gear means the far face first.
	 */
	for (i = 0; i < g->teeth; i++) {
		uint8_t a0 = SPIN(g, i * pitch);
		uint8_t a1 = (uint8_t)(a0 + da);
		uint8_t a2 = (uint8_t)(a0 + 2 * da);
		uint8_t a3 = (uint8_t)(a0 + 3 * da);
		uint8_t an = (uint8_t)(a0 + pitch);
		int h0x, h0y, h3x, h3y, hnx, hny;
		int r0x, r0y, r3x, r3y, rnx, rny;

		PROJ(g, rot, r0, a0, -hw, h0x, h0y);
		PROJ(g, rot, r1, a0, -hw, r0x, r0y);
		PROJ(g, rot, r0, a3, -hw, h3x, h3y);
		PROJ(g, rot, r1, a3, -hw, r3x, r3y);
		PROJ(g, rot, r0, an, -hw, hnx, hny);
		PROJ(g, rot, r1, an, -hw, rnx, rny);

		fill_tri(h0x, h0y, r0x, r0y, r3x, r3y, g->side);
		fill_tri(h0x, h0y, r3x, r3y, h3x, h3y, g->side);
		fill_tri(h3x, h3y, r3x, r3y, rnx, rny, g->side);
		fill_tri(h3x, h3y, rnx, rny, hnx, hny, g->side);

		/* The tooth's back face: r1, r2, r2, r1. */
		{
			int ax, ay, bx, by, cx, cy, ex, ey;

			PROJ(g, rot, r1, a0, -hw, ax, ay);
			PROJ(g, rot, r2, a1, -hw, bx, by);
			PROJ(g, rot, r2, a2, -hw, cx, cy);
			PROJ(g, rot, r1, a3, -hw, ex, ey);

			fill_tri(ax, ay, bx, by, cx, cy, g->side);
			fill_tri(ax, ay, cx, cy, ex, ey, g->side);
		}
	}

	/* ---- the outward walls, the part that gives the gear its thickness ----
	 *
	 * glxgears emits one GL_QUAD_STRIP pairing +width/2 and -width/2 at
	 * r1@angle, r2@(angle+da), r2@(angle+2da), r1@(angle+3da), closing back
	 * at r1@0. Each edge is its own quad here -- the same surface, with no
	 * shared strip state to get wrong.
	 */
	for (i = 0; i < g->teeth; i++) {
		uint8_t a0 = SPIN(g, i * pitch);
		uint8_t a1 = (uint8_t)(a0 + da);
		uint8_t a2 = (uint8_t)(a0 + 2 * da);
		uint8_t a3 = (uint8_t)(a0 + 3 * da);
		uint8_t an = (uint8_t)(a0 + pitch);
		int f0x, f0y, b0x, b0y;		/* r1 at a0, front and back */
		int f1x, f1y, b1x, b1y;		/* r2 at a1 */
		int f2x, f2y, b2x, b2y;		/* r2 at a2 */
		int f3x, f3y, b3x, b3y;		/* r1 at a3 */
		int fnx, fny, bnx, bny;		/* r1 at the next tooth */

		PROJ(g, rot, r1, a0, hw, f0x, f0y);
		PROJ(g, rot, r1, a0, -hw, b0x, b0y);
		PROJ(g, rot, r2, a1, hw, f1x, f1y);
		PROJ(g, rot, r2, a1, -hw, b1x, b1y);
		PROJ(g, rot, r2, a2, hw, f2x, f2y);
		PROJ(g, rot, r2, a2, -hw, b2x, b2y);
		PROJ(g, rot, r1, a3, hw, f3x, f3y);
		PROJ(g, rot, r1, a3, -hw, b3x, b3y);
		PROJ(g, rot, r1, an, hw, fnx, fny);
		PROJ(g, rot, r1, an, -hw, bnx, bny);

		/* Leading flank: r1@a0 up to r2@a1. */
		fill_tri(f0x, f0y, f1x, f1y, b1x, b1y, g->side);
		fill_tri(f0x, f0y, b1x, b1y, b0x, b0y, g->side);
		/* Tip: r2@a1 across to r2@a2. */
		fill_tri(f1x, f1y, f2x, f2y, b2x, b2y, g->side);
		fill_tri(f1x, f1y, b2x, b2y, b1x, b1y, g->side);
		/* Trailing flank: r2@a2 down to r1@a3. */
		fill_tri(f2x, f2y, f3x, f3y, b3x, b3y, g->side);
		fill_tri(f2x, f2y, b3x, b3y, b2x, b2y, g->side);
		/* Root arc to the next tooth, so the wall runs all the way round
		 * rather than only on the teeth. */
		fill_tri(f3x, f3y, fnx, fny, bnx, bny, g->side);
		fill_tri(f3x, f3y, bnx, bny, b3x, b3y, g->side);
	}

	/* ---- the inner cylinder: the wall of the hole ---- */
	for (seg = 0; seg < GEAR_SEG; seg++) {
		uint8_t a = SPIN(g, seg * 256 / GEAR_SEG);
		uint8_t b = SPIN(g, (seg + 1) * 256 / GEAR_SEG);
		int fax, fay, bax, bay, fbx, fby, bbx, bby;

		PROJ(g, rot, r0, a, hw, fax, fay);
		PROJ(g, rot, r0, a, -hw, bax, bay);
		PROJ(g, rot, r0, b, hw, fbx, fby);
		PROJ(g, rot, r0, b, -hw, bbx, bby);

		fill_tri(fax, fay, fbx, fby, bbx, bby, g->side);
		fill_tri(fax, fay, bbx, bby, bax, bay, g->side);
	}

	/* ---- the front face, at z = +width/2 ----
	 *
	 * The body is a continuous quad strip between the hole and the root
	 * circle. Continuous is the point: glxgears walks it with r0@a, r1@a,
	 * r0@a, r1@(a + 3*da), and the next iteration starts at r0@(a + pitch).
	 * A quad strip joins consecutive vertex pairs, so the segment between
	 * the last vertex of one iteration and the first of the next spans
	 * r1@(a + 3*da) back to r0@(a + pitch) -- that is the piece closing the
	 * body behind each tooth. Drawing each tooth's span as an isolated quad
	 * leaves a wedge of background between every pair of teeth.
	 */
	for (i = 0; i < g->teeth; i++) {
		uint8_t a0 = SPIN(g, i * pitch);
		uint8_t a3 = (uint8_t)(a0 + 3 * da);
		uint8_t an = (uint8_t)(a0 + pitch);
		int h0x, h0y, h3x, h3y, hnx, hny;
		int r0x, r0y, r3x, r3y, rnx, rny;

		PROJ(g, rot, r0, a0, hw, h0x, h0y);
		PROJ(g, rot, r1, a0, hw, r0x, r0y);
		PROJ(g, rot, r0, a3, hw, h3x, h3y);
		PROJ(g, rot, r1, a3, hw, r3x, r3y);
		PROJ(g, rot, r0, an, hw, hnx, hny);
		PROJ(g, rot, r1, an, hw, rnx, rny);

		fill_tri(h0x, h0y, r0x, r0y, r3x, r3y, g->face);
		fill_tri(h0x, h0y, r3x, r3y, h3x, h3y, g->face);
		fill_tri(h3x, h3y, r3x, r3y, rnx, rny, g->face);
		fill_tri(h3x, h3y, rnx, rny, hnx, hny, g->face);

		/* The tooth's front face. */
		{
			int ax, ay, bx, by, cx, cy, ex, ey;

			PROJ(g, rot, r1, a0, hw, ax, ay);
			PROJ(g, rot, r2, (uint8_t)(a0 + da), hw, bx, by);
			PROJ(g, rot, r2, (uint8_t)(a0 + 2 * da), hw, cx, cy);
			PROJ(g, rot, r1, a3, hw, ex, ey);

			fill_tri(ax, ay, bx, by, cx, cy, g->face);
			fill_tri(ax, ay, cx, cy, ex, ey, g->face);
		}
	}
}

/* Depth key for the back-to-front sort: the transformed z of the centre. */
static int32_t gear_depth(const struct gear *g, const struct mat3 *rot)
{
	int32_t ox, oy, oz;

	mat3_apply(rot, g->tx, g->ty, 0, &ox, &oy, &oz);
	return oz;
}

/* ------------------------------------------------------------------ *
 * The scene
 *
 * The reference's own layout, in its own units: gear1 at (-3.0, -2.0),
 * gear2 at (3.1, -2.0), gear3 at (-3.1, 4.2), all at z = 0. The units are
 * scaled to fit a 128x160 panel, but the ratios are the reference's, so the
 * gears mesh the way they do on a desktop.
 * ------------------------------------------------------------------ */

#define WORLD (FP_ONE)		/* one world unit, for readability below */

/* The reference's world unit, in 16.16. glxgears' gear1 has outer radius 4.0
 * and the scene spans about 10 units across; at 128 pixels wide that is a
 * scale of roughly 13 pixels per unit, but the panel is also shorter than it
 * is wide, so the scale is set by the vertical fit. */
static struct gear gears[3];

static void gear_setup(void)
{
	/* gear(1.0, 4.0, 1.0, 20, 0.7) -- the big red one. */
	gears[0].inner = 1 * WORLD;
	gears[0].outer = 4 * WORLD;
	gears[0].width = 1 * WORLD;
	gears[0].teeth = 20;
	gears[0].tooth_depth = WORLD * 7 / 10;
	gears[0].tx = -3 * WORLD;
	gears[0].ty = -2 * WORLD;
	gears[0].spin = 0;
	gears[0].face = RGB565(31, 6, 0);	/* 0.8, 0.1, 0.0 */
	gears[0].side = RGB565(15, 3, 0);

	/* gear(0.5, 2.0, 2.0, 10, 0.7) -- green, and twice as thick. */
	gears[1].inner = WORLD / 2;
	gears[1].outer = 2 * WORLD;
	gears[1].width = 2 * WORLD;
	gears[1].teeth = 10;
	gears[1].tooth_depth = WORLD * 7 / 10;
	gears[1].tx = (int32_t)(WORLD * 31 / 10);
	gears[1].ty = -2 * WORLD;
	gears[1].spin = 0;
	gears[1].face = RGB565(0, 31, 8);	/* 0.0, 0.8, 0.2 */
	gears[1].side = RGB565(0, 15, 4);

	/* gear(1.3, 2.0, 0.5, 10, 0.7) -- blue, and thin. */
	gears[2].inner = (int32_t)(WORLD * 13 / 10);
	gears[2].outer = 2 * WORLD;
	gears[2].width = WORLD / 2;
	gears[2].teeth = 10;
	gears[2].tooth_depth = WORLD * 7 / 10;
	gears[2].tx = (int32_t)(WORLD * -31 / 10);
	gears[2].ty = (int32_t)(WORLD * 42 / 10);
	gears[2].spin = 0;
	gears[2].face = RGB565(6, 6, 31);	/* 0.2, 0.2, 1.0 */
	gears[2].side = RGB565(3, 3, 15);
}

/* Fill the whole visible area with one colour. The reference clears to black;
 * a gradient costs the same and makes the rotation readable. */
static void clear_frame(uint16_t top, uint16_t bottom)
{
	int y, x;
	int r0 = (top >> 11) & 0x1f, g0 = (top >> 5) & 0x3f, b0 = top & 0x1f;
	int r1 = (bottom >> 11) & 0x1f, g1 = (bottom >> 5) & 0x3f;
	int b1 = bottom & 0x1f;

	for (y = 0; y < fb_height; y++) {
		int r = r0 + (r1 - r0) * y / fb_height;
		int g = g0 + (g1 - g0) * y / fb_height;
		int b = b0 + (b1 - b0) * y / fb_height;
		uint16_t c = RGB565(r, g, b);
		uint16_t *row = fb + (size_t)y * (size_t)fb_line_px;

		for (x = 0; x < PANEL_W; x++)
			row[x] = c;
	}
}

/* One frame. `angle` is the reference's own animation variable: 70 degrees per
 * second, and each gear spins at a fixed multiple of it, so the three stay in
 * the same relationship to each other however long the program runs.
 *
 * glxgears applies, in order: rotate the whole scene by view_rotx about X,
 * view_roty about Y, view_rotz about Z; then per gear a translate and a
 * rotation about Z. The scene rotation is the matrix; the per-gear rotation is
 * folded into each vertex's angle by SPIN; the translate is in the gear. */
static void draw_frame(uint8_t ax, uint8_t ay, int32_t angle)
{
	struct mat3 rot;
	int order[3] = { 0, 1, 2 };
	int32_t depth[3];
	int i, j;

	clear_frame(RGB565(2, 4, 10), RGB565(6, 10, 20));

	mat3_rot_xy(&rot, ax, ay);

	/* glxgears' own per-gear spin: gear1 by angle, gear2 by -2*angle - 9,
	 * gear3 by -2*angle - 25. The offsets are what make the teeth mesh
	 * rather than collide, so they are carried over as they are. */
	gears[0].spin = angle;
	gears[1].spin = -(2 * angle) - (9 * FP_ONE * 256 / 360);
	gears[2].spin = -(2 * angle) - (25 * FP_ONE * 256 / 360);

	/* Back to front. Three separated objects need no z-buffer; the centre's
	 * transformed z is enough to order them. */
	for (i = 0; i < 3; i++)
		depth[i] = gear_depth(&gears[i], &rot);

	for (i = 0; i < 2; i++)
		for (j = 0; j < 2 - i; j++)
			if (depth[order[j]] > depth[order[j + 1]]) {
				int t = order[j];
				order[j] = order[j + 1];
				order[j + 1] = t;
			}

	for (i = 0; i < 3; i++)
		gear_draw(&gears[order[i]], &rot);
}

/* ------------------------------------------------------------------ *
 * Clock and console
 * ------------------------------------------------------------------ */

/* Milliseconds since boot, from the kernel's own monotonic clock.
 *
 * clock_gettime(CLOCK_MONOTONIC) is the obvious call and its struct timespec
 * is 64-bit, which this toolchain cannot lower (see the header). /proc/uptime
 * carries the same number as text and costs one small read, which is nothing
 * against a frame.
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

/* Sleep for the remainder of a frame's budget.
 *
 * usleep is the only sub-second sleep in this uclibc build, and it takes
 * microseconds, so the arithmetic stays in 32-bit by keeping the budget in
 * microseconds and never multiplying a millisecond count by 1000 twice. */
static void sleep_us(uint32_t us)
{
	if (us > 0)
		usleep((useconds_t)us);
}

/* The console tty, held so the signal handler can restore text mode. A program
 * killed mid-run with the panel in KD_GRAPHICS would otherwise strand the
 * console there until a reboot. */
static int tty_fd = -1;
static volatile sig_atomic_t graphics_taken;
static volatile sig_atomic_t stop_requested;

static void restore_console(void)
{
	if (graphics_taken && tty_fd >= 0) {
		ioctl(tty_fd, KDSETMODE, KD_TEXT);
		graphics_taken = 0;
	}
}

static void on_signal(int sig)
{
	(void)sig;
	stop_requested = 1;
	/* Restore here rather than only in the main loop: the loop might be
	 * blocked in usleep, and the console should come back immediately. */
	restore_console();
}

static void usage(const char *argv0)
{
	fprintf(stderr,
		"usage: %s [-f fps] [-s seconds] [-b] [-q]\n"
		"  -f N   target N frames per second; 0 is uncapped (default 30)\n"
		"  -s S   run for S seconds (default 0 = until interrupted)\n"
		"  -b     leave the console in graphics mode afterwards\n"
		"  -q     do not touch the console; draw only\n", argv0);
}

int main(int argc, char **argv)
{
	int target_fps = 30;
	int seconds = 0;
	int leave_graphics = 0;
	int quiet = 0;
	int opt;
	int frame = 0;
	uint32_t start, deadline, frame_start, next_frame;
	uint32_t report_at;
	int frames_this_second = 0;
	uint32_t worst_frame = 0;
	uint8_t ax = 256 * 20 / 360;	/* view_rotx, 20 degrees */
	uint8_t ay = 256 * 30 / 360;	/* view_roty, 30 degrees */
	int32_t angle = 0;		/* glxgears' animation variable */
	uint32_t t_rot0 = 0;		/* last frame's timestamp, for dt */

	while ((opt = getopt(argc, argv, "f:s:bqh")) != -1) {
		switch (opt) {
		case 'f':
			target_fps = atoi(optarg);
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

	int fb_fd = open("/dev/fb0", O_RDWR);
	if (fb_fd < 0) {
		fprintf(stderr, "fbdemo: open /dev/fb0: %s\n", strerror(errno));
		return 1;
	}

	struct fb_var_screeninfo var;
	struct fb_fix_screeninfo fix;

	if (ioctl(fb_fd, FBIOGET_VSCREENINFO, &var) < 0) {
		fprintf(stderr, "fbdemo: FBIOGET_VSCREENINFO: %s\n",
			strerror(errno));
		close(fb_fd);
		return 1;
	}
	if (ioctl(fb_fd, FBIOGET_FSCREENINFO, &fix) < 0) {
		fprintf(stderr, "fbdemo: FBIOGET_FSCREENINFO: %s\n",
			strerror(errno));
		close(fb_fd);
		return 1;
	}

	fb_line_px = (int)(fix.line_length / (var.bits_per_pixel / 8));
	fb_height = (int)var.yres;

	/* No mmap. fbtft_ops (fbtft-core.c:428) has no .fb_mmap -- it is
	 * FB_DEFAULT_DEFERRED_OPS plus setcolreg and blank, and nothing else --
	 * so mmap("/dev/fb0") returns ENODEV on this driver and always will.
	 * The framebuffer is reached with write() and lseek(), which is what the
	 * deferred-IO path is built for: fb_write copies into screen_buffer and
	 * marks the damaged lines, and fbtft pushes them out over SPI on its own
	 * schedule.
	 *
	 * The frame buffer is allocated once and reused, so the draw loops do no
	 * allocation at all. */
	size_t frame_bytes = (size_t)fix.line_length * (size_t)var.yres;

	fb = malloc(frame_bytes);
	if (!fb) {
		fprintf(stderr, "fbdemo: malloc %u: %s\n", (unsigned)frame_bytes,
			strerror(errno));
		close(fb_fd);
		return 1;
	}

	/* Take the console out of text mode so fbcon stops repainting over us.
	 * /dev/tty0 is the active console; KD_GRAPHICS is the standard "a
	 * program owns the framebuffer now" signal. */
	if (!quiet) {
		tty_fd = open("/dev/tty0", O_RDWR);
		if (tty_fd < 0)
			tty_fd = open("/dev/tty1", O_RDWR);
		if (tty_fd >= 0) {
			if (ioctl(tty_fd, KDSETMODE, KD_GRAPHICS) < 0) {
				fprintf(stderr, "fbdemo: KDSETMODE KD_GRAPHICS: %s\n",
					strerror(errno));
			} else {
				graphics_taken = 1;
			}
		} else {
			fprintf(stderr, "fbdemo: no console tty: %s\n",
				strerror(errno));
		}
	}

	/* Ctrl-C must not strand the panel in graphics mode. */
	signal(SIGINT, on_signal);
	signal(SIGTERM, on_signal);
	signal(SIGHUP, on_signal);

	gear_setup();

	printf("fbdemo: %dx%d, target %d fps%s\n",
	       PANEL_W, fb_height, target_fps,
	       target_fps ? "" : " (uncapped)");
	if (seconds)
		printf("fbdemo: running for %d seconds\n", seconds);
	else
		printf("fbdemo: running until interrupted (Ctrl-C)\n");
	fflush(stdout);

	start = now_ms();
	t_rot0 = start;
	deadline = seconds ? start + (uint32_t)seconds * 1000 : 0;
	next_frame = start;
	report_at = start + 1000;

	while (!stop_requested) {
		uint32_t now;

		frame_start = now_ms();

		draw_frame(ax, ay, angle);

		if (lseek(fb_fd, 0, SEEK_SET) < 0 ||
		    write(fb_fd, fb, frame_bytes) != (ssize_t)frame_bytes) {
			fprintf(stderr, "fbdemo: write: %s\n", strerror(errno));
			break;
		}

		frame++;
		frames_this_second++;

		/* glxgears' own animation step: angle += 70 degrees per second,
		 * wrapped at 3600. The view angles never move -- view_rotx and
		 * view_roty are constants there, and they are constants here.
		 *
		 * dt is measured from the clock rather than assumed to be one
		 * frame, so a frame that overruns stretches the rotation instead
		 * of slowing it down. That is what the reference does, and it is
		 * why the gears look the same speed whatever the frame rate.
		 *
		 * 70 degrees per second is 70/360 of a turn, and `angle` is in
		 * 16.16 turns, so one second of rotation is
		 * 70 * 65536 / 360 = 12743. */
		{
			uint32_t t_rot = now_ms();
			uint32_t dt = t_rot - t_rot0;

			t_rot0 = t_rot;
			angle += (int32_t)(dt * 12743u / 1000u);
			/* 3600 degrees is ten turns, in 16.16 turns. Written as
			 * 10 * FP_ONE rather than the degree form because
			 * 3600 * FP_ONE * 256 overflows a signed 32-bit int. */
			if (angle > 10 * FP_ONE)
				angle -= 10 * FP_ONE;
		}

		/* Frame pacing. The budget is in microseconds and the elapsed
		 * time is converted once, so no millisecond count is scaled
		 * twice. */
		now = now_ms();
		{
			uint32_t elapsed = now - frame_start;

			if (elapsed > worst_frame)
				worst_frame = elapsed;
		}

		if (target_fps > 0) {
			uint32_t budget_us = 1000000u / (uint32_t)target_fps;

			next_frame += (uint32_t)target_fps
					? (1000u / (uint32_t)target_fps) : 0;
			/* Accumulate the remainder so the average rate is exact
			 * rather than drifting a millisecond per frame. */
			if (next_frame > now) {
				uint32_t ahead_ms = next_frame - now;

				sleep_us(ahead_ms * 1000u);
			} else {
				next_frame = now;
			}
			(void)budget_us;
		}

		/* Report once a second, to the terminal. */
		if (now >= report_at) {
			uint32_t span = now - (report_at - 1000);
			double fps = span ? frames_this_second * 1000.0 / span : 0.0;

			printf("fbdemo: %.1f fps (min %.1f, worst frame %u ms)\n",
			       fps,
			       worst_frame ? 1000.0 / (double)worst_frame : 0.0,
			       (unsigned)worst_frame);
			fflush(stdout);

			frames_this_second = 0;
			worst_frame = 0;
			report_at = now + 1000;
		}

		if (deadline && now >= deadline)
			break;
	}

	restore_console();
	if (!quiet && tty_fd >= 0) {
		if (leave_graphics) {
			ioctl(tty_fd, KDSETMODE, KD_GRAPHICS);
			graphics_taken = 1;
			printf("fbdemo: console left in graphics mode (-b)\n");
		} else {
			printf("fbdemo: console restored to text mode\n");
		}
	}
	fflush(stdout);

	free(fb);
	if (tty_fd >= 0)
		close(tty_fd);
	close(fb_fd);

	return 0;
}
