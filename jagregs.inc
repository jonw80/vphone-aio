;
; jagregs.inc -- the subset of Atari's jaguar.inc that init.s uses.
;
; jaguar.inc was not part of the source release; these addresses are from
; the Jaguar Technical Reference Manual rev 8 (ROMHI memory map).
;
G_END		equ	$f0210c		; GPU data organisation (TRM p.59)
D_END		equ	$f1a10c		; DSP data organisation
OLP			equ	$f00020		; object list pointer (TRM p.12)
VMODE		equ	$f00028		; video mode
BORD1		equ	$f0002a		; border colour red/green
HDB1		equ	$f00038		; horizontal display begin 1
HDB2		equ	$f0003a		; horizontal display begin 2
HDE			equ	$f0003c		; horizontal display end
VDB			equ	$f00046		; vertical display begin
VDE			equ	$f00048		; vertical display end
VI			equ	$f0004e		; vertical interrupt line
BG			equ	$f00058		; background colour
INT1		equ	$f000e0		; CPU interrupt control
INT2		equ	$f000e2		; CPU interrupt resume
JOYSTICK	equ	$f14000		; joystick port (TRM p.95)
CONFIG		equ	$f14002		; joystick buttons + config bits (bit 4: NTSC)
USER0		equ	$100		; 68000 vector $40, used for all TOM interrupts (TRM bug #7)
ENDRAM		equ	$200000		; top of the 2 Mbyte DRAM
