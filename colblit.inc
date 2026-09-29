;
; colblit.inc -- blitter setup shared by the wall (r_phase6) and sprite
; (r_phase8) column drawers.
;
; The shipped code draws each column pixel as one pass of the blitter's
; OUTER loop (inner count 1), so it can advance the texture and screen
; pointers with the step registers.  Every pass pays for three pointer
; updates (UPDA1F, UPDA1, UPDA2 -- "a one-tick overhead per update", TRM
; p.73) plus the loop turnaround.
;
; With FASTCOLUMNS the whole column is ONE inner loop:
;   A1 (texture, source) uses X add = increment, the DDA mode the manual's
;      image-rotation example uses for a scaled source with DSTA2 (TRM
;      pp.71, 79), so the 16.16 texture step lives in A1_INC/A1_FINC.
;   A2 (screen, destination) uses X add = zero with Y add = 1, so it moves
;      down one line per pixel.
; TRM bug #11: the Y add bits of A1 and A2 are not decoded separately;
; A1's bit drives both, and the documented work-around is to set it on
; both.  A1 ignores its Y add bit in add-increment mode (TRM p.71), so
; setting it there costs nothing.
;
; Set FASTCOLUMNS to 0 to assemble the shipped per-pixel outer-loop blits.
;
; NB: Madmac/rmac evaluate expressions strictly left to right, with no
; operator precedence ("1+2*3" is 9), so keep every expression here to a
; single kind of operator.
;
FASTCOLUMNS	.equ	1

BLIT_SRCEN		.equ	1
BLIT_UPDA1F		.equ	1<<8
BLIT_UPDA1		.equ	1<<9
BLIT_UPDA2		.equ	1<<10
BLIT_DSTA2		.equ	1<<11
BLIT_GOURZ		.equ	1<<13		; needed for SRCSHADE (TRM bug #9)
BLIT_LFU_S		.equ	12<<21		; write source data
BLIT_SRCSHADE	.equ	1<<30

; flags: 16 bpp, texture window 128 wide (only X is used, as a linear
; offset), screen window 160 wide
FLAGS_16BPP		.equ	4<<3
FLAGS_W128		.equ	$1c<<9
FLAGS_W160		.equ	$1d<<9
FLAGS_XPHRASE	.equ	0<<16
FLAGS_XPIXEL	.equ	1<<16
FLAGS_XZERO		.equ	2<<16
FLAGS_XINC		.equ	3<<16
FLAGS_YADD1		.equ	1<<18

; blitter register indices for (r14/r15+n) addressing from $F02200
BR_A1PIX		.equ	3
BR_A1STEP		.equ	4
BR_A1FSTEP		.equ	5
BR_A1FRAC		.equ	6
BR_A1INC		.equ	7
BR_A1FINC		.equ	8
BR_A2PIX		.equ	12
BR_CMD			.equ	14
BR_COUNT		.equ	15

	.if FASTCOLUMNS
COL_A1FLAGS		.equ	FLAGS_16BPP+FLAGS_W128+FLAGS_XINC+FLAGS_YADD1
COL_A1SCREEN	.equ	FLAGS_16BPP+FLAGS_W160+FLAGS_XINC+FLAGS_YADD1
COL_A2FLAGS		.equ	FLAGS_16BPP+FLAGS_W160+FLAGS_XZERO+FLAGS_YADD1
COL_CMD			.equ	BLIT_SRCEN+BLIT_DSTA2+BLIT_GOURZ+BLIT_SRCSHADE+BLIT_LFU_S
COL_A1STEP		.equ	BR_A1INC
COL_A1FSTEP		.equ	BR_A1FINC
COL_A1STEP_REG	.equ	$f0221c		; A1 increment
COL_A1FSTEP_REG	.equ	$f02220		; A1 increment fraction
	.else
COL_A1FLAGS		.equ	FLAGS_16BPP+FLAGS_W128+FLAGS_XZERO			; 145440
COL_A1SCREEN	.equ	FLAGS_16BPP+FLAGS_W160+FLAGS_XZERO			; 145952
COL_A2FLAGS		.equ	FLAGS_16BPP+FLAGS_W160+FLAGS_XZERO			; 145952
COL_CMD			.equ	BLIT_SRCEN+BLIT_UPDA1F+BLIT_UPDA1+BLIT_UPDA2+BLIT_DSTA2+BLIT_GOURZ+BLIT_SRCSHADE+BLIT_LFU_S
COL_A1STEP		.equ	BR_A1STEP
COL_A1FSTEP		.equ	BR_A1FSTEP
COL_A1STEP_REG	.equ	$f02210		; A1 step
COL_A1FSTEP_REG	.equ	$f02214		; A1 step fraction
	.endif
