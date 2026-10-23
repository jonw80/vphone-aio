/* r_detail.c -- the view's horizontal resolution, chosen at run time */

#include "doomdef.h"
#include "r_local.h"

/*
 * The shipped refresh drew 160 columns, shown 2 pixels wide by a 160-pixel
 * video mode.  The detail setting picks how many columns the GPU draws:
 *
 *   detail_low    160  the original (video PWIDTH 8)
 *   detail_high   320  one column per pixel of the 320-pixel mode the
 *                      status bar and menus already use (PWIDTH 4)
 *   detail_ultra  640  twice that, in the 640-pixel mode (PWIDTH 2); more
 *                      than real hardware can keep up with, for emulators
 *
 * The status bar is 320 wide.  With a 160 or 320 view the GPU's object
 * interrupt switches the video mode to 320 pixels below the view, as the
 * shipped game did.  A 640 view keeps its mode for the whole frame and the
 * status bar objects become scaled ones that write each pixel twice:
 * emulators such as Virtual Jaguar size their output from one video mode
 * per frame, so a 640 top over a 320 bottom would show half the view.
 *
 * Only the horizontal projection changes.  The view stays 180 lines with
 * the same vertical scale (walls: R_ScaleFromGlobalAngle's 22*8, flats:
 * yslope[]), so sprites get yscale = xscale * stretchx with stretchx
 * shrinking as the width grows.  The angle tables are made here with the
 * same arithmetic that produced the shipped 160-column ones (tables.c used
 * to carry those; R_SetDetail(detail_low) reproduces them bit for bit).
 */

int			detailmode = -1;
int			detailsetting;			/* detail_low etc, saved in the EEPROM */
int			screenwidth;			/* columns drawn: 160, 320 or 640 */
int			centerx;				/* screenwidth/2 */
fixed_t		centerxfrac;			/* centerx<<FRACBITS, the projection */
fixed_t		stretchx;				/* sprite yscale = xscale*stretchx */
int			screenwid;				/* blitter WID code for screenwidth */
int			col_a1screen, col_a2flags, plane_a2flags;	/* blitter flags */
int			viewvmode;				/* VMODE value for the view */
int			viewpwidth;				/* view width in phrases */
int			viewxpos;				/* view object xpos */
int			viewshift;				/* log2(screenwidth/160) */
int			sbarscaled;				/* status bar objects scaled 2x across */
int			viewsbarvmode;			/* VMODE below the view: a frame takes it */
									/* to gpubase.gas's sbarvmode as it goes up */

int			viewangletox[FINEANGLES/2];
angle_t		xtoviewangle[MAXSCREENWIDTH+1];
unsigned short	distscale[MAXSCREENWIDTH];	/* 1.15 frac */

/* r_phase6's clipbounds[] and r_phase8's spropening[] live in GPU RAM, */
/* which has room for 160 of them.  Wider views keep them in main RAM. */
extern	int	clipbounds[], spropening[];		/* in the GPU overlays */
int			wideclipbounds[MAXSCREENWIDTH];
int			widespropening[MAXSCREENWIDTH];
int			*clipbounds_p, *spropening_p;	/* what the GPU uses */

#ifdef JAGUAR
/* r_phase6/7 step through visplanes[] by VISPLANE_SIZE (colblit.inc) */
typedef char visplanesize_check[sizeof(visplane_t) == 1308 ? 1 : -1];
#endif

#define	FIELDOFVIEW_ANGLES	2048		/* 90 degrees of the 8192 fine angles */

/* 16x16 bit unsigned multiply: one mulu.w, where gcc makes a 32 bit one */
/* (a call to __mulsi3) even out of zero extended shorts */
#ifdef JAGUAR
static unsigned MULU (unsigned x, unsigned y)
{
	__asm__ ("mulu.w %1,%0" : "+d" (x) : "d" (y));
	return x;
}
#else
#define	MULU(x,y)	((unsigned)(unsigned short)(x) * (unsigned short)(y))
#endif

/* FixedMul rounding toward minus infinity, like the >>16 of a 64-bit */
/* product the shipped tables were made with (FixedMul truncates) */
static fixed_t FloorMul (fixed_t a, fixed_t b)
{
	int			sign;
	unsigned	a1, a2, b1, b2, lo, c;

	sign = a^b;
	if (a < 0)
		a = -a;
	if (b < 0)
		b = -b;
	a1 = a&0xffff;
	a2 = (unsigned)a>>16;
	b1 = b&0xffff;
	b2 = (unsigned)b>>16;
	lo = MULU(a1,b1);
	c = lo>>16;
	c += MULU(a2,b1);
	c += MULU(b2,a1);
	c += MULU(b2,a2)<<16;
	if (sign < 0)
	{
		c = -c;
		if (lo&0xffff)
			c--;			/* the product had a fraction: round down */
	}
	return c;
}

static const struct
{
	short	width, wid, vmode;
} detailmodes[NUMDETAILS] =
{
	{ 160, 0x1d, 0xc1+(7<<9) },		/* 1.25 * 2^7, PWIDTH 8 */
	{ 320, 0x21, 0xc1+(3<<9) },		/* 1.25 * 2^8, PWIDTH 4 */
	{ 640, 0x25, 0xc1+(1<<9) }		/* 1.25 * 2^9, PWIDTH 2 */
};

void R_SetDetail (int mode)
{
	int			i, x, t;
	fixed_t		focallength;
	int			cosadj;

	if ((unsigned)mode >= NUMDETAILS)
		mode = detail_low;
	if (mode == detailmode)
		return;
	detailmode = mode;

	screenwidth = detailmodes[mode].width;
	screenwid = detailmodes[mode].wid;
	viewvmode = detailmodes[mode].vmode;
	viewshift = mode;
	centerx = screenwidth/2;
	centerxfrac = centerx<<FRACBITS;
	stretchx = STRETCH>>viewshift;
	viewpwidth = screenwidth/4;
	viewxpos = BASEORGX<<viewshift;
	sbarscaled = mode == detail_ultra;
	viewsbarvmode = sbarscaled ? viewvmode : 0xc1+(3<<9);

	/* 16 bpp, screen window, as in colblit.inc */
	col_a1screen = (4<<3) + (screenwid<<9) + (3<<16) + (1<<18);
	col_a2flags = (4<<3) + (screenwid<<9) + (2<<16) + (1<<18);
	plane_a2flags = (4<<3) + (screenwid<<9) + (1<<16);

	/* screen x for every view angle, then the angle at every column */
	focallength = FixedDiv (centerxfrac,
		finetangent[FINEANGLES/4+FIELDOFVIEW_ANGLES/2]);
	for (i=0 ; i<FINEANGLES/2 ; i++)
	{
		if (finetangent[i] > FRACUNIT*2)
			t = -1;
		else if (finetangent[i] < -FRACUNIT*2)
			t = screenwidth+1;
		else
		{
			t = FloorMul (finetangent[i], focallength);
			t = (centerxfrac - t + FRACUNIT-1)>>FRACBITS;
			if (t < -1)
				t = -1;
			else if (t > screenwidth+1)
				t = screenwidth+1;
		}
		viewangletox[i] = t;
	}
	/* the first angle that maps to x or further left: viewangletox[] */
	/* never increases, so it only moves back as x grows */
	i = 0;
	while (viewangletox[i] > 0)
		i++;
	for (x=0 ; x<=screenwidth ; x++)
	{
		while (i > 0 && viewangletox[i-1] <= x)
			i--;
		xtoviewangle[x] = (i<<ANGLETOFINESHIFT) - ANG90;
	}
	for (i=0 ; i<FINEANGLES/2 ; i++)
	{
		if (viewangletox[i] == -1)
			viewangletox[i] = 0;
		else if (viewangletox[i] == screenwidth+1)
			viewangletox[i] = screenwidth;
	}

	/* 1/cos of each column's angle, for flat distances */
	for (x=0 ; x<screenwidth ; x++)
	{
		cosadj = finecosine[xtoviewangle[x]>>ANGLETOFINESHIFT];
		if (cosadj < 0)
			cosadj = -cosadj;
		distscale[x] = FixedDiv (FRACUNIT, cosadj)>>1;
	}

	clipangle = xtoviewangle[0];
	doubleclipangle = clipangle*2;

	if (mode == detail_low)
	{
		clipbounds_p = clipbounds;
		spropening_p = spropening;
	}
	else
	{
		clipbounds_p = wideclipbounds;
		spropening_p = widespropening;
	}
}
