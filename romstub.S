| romstub.S -- cartridge entry: copy doom.bin to $4000, clear BSS, start it.
|
| The program is linked to run from RAM at $4000 (see README in the source
| release); the cartridge holds it right after this stub.  tools/mkrom.py
| assembles this with:
|   IMG_LONGS  length of doom.bin in longs
|   BSS_LONGS  longs from the end of doom.bin to _enddata
| Position independent apart from the absolute load address.

	.text
	.globl	romstub
romstub:
	move.w	#0x2700,%sr			| no interrupts until init.s sets them up
	lea	image(%pc),%a0
	lea	0x4000,%a1
	move.l	#IMG_LONGS,%d0
1:	move.l	(%a0)+,(%a1)+
	subq.l	#1,%d0
	bne.s	1b
	move.l	#BSS_LONGS,%d0
	beq.s	3f
2:	clr.l	(%a1)+
	subq.l	#1,%d0
	bne.s	2b
3:	jmp	0x4000

	.balign	4
image:
