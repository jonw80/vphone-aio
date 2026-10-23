| lib68k.S -- the runtime support modern gcc may call, for a plain 68000.
|
| The distribution libgcc for m68k is built for the 68020 (32-bit mulu.l /
| divu.l do not exist on the Jaguar's 68000), so it is not linked.  The
| shipped game's 32-bit multiply/divide helpers are C in jagonly.c, but a
| modern gcc compiles them into calls to themselves, so they are here in
| assembly with the same semantics -- quirks included, since the game was
| tested with them:
|   __mulsi3   low 32 bits of the product
|   __udivsi3  0x7fffffff whenever (a >> 30) >= b (also covers b == 0),
|              otherwise the exact quotient
|   __divsi3   that, on magnitudes, with the sign of a^b
|   __umodsi3  a - __udivsi3(a,b)*b
|   __modsi3   the same, treating its arguments as unsigned
| plus memcpy/memset, which modern gcc emits for structure copies.  Both
| underscore spellings are provided because the C code is built with
| -fleading-underscore.  gcc m68k ABI: arguments on the stack, result in
| d0, d0-d1/a0-a1 may be clobbered.

	.text
	.even

| mul32: d0 = low 32 bits of d0 * d1; clobbers d1, preserves the rest
mul32:
	move.l	%d2,-(%sp)
	move.l	%d0,%d2
	swap	%d2
	mulu.w	%d1,%d2			| a.hi * b.lo
	move.l	%d1,-(%sp)
	swap	%d1
	mulu.w	%d0,%d1			| b.hi * a.lo
	add.w	%d1,%d2
	swap	%d2
	clr.w	%d2
	move.l	(%sp)+,%d1
	mulu.w	%d1,%d0			| a.lo * b.lo
	add.l	%d2,%d0
	move.l	(%sp)+,%d2
	rts

	.globl	__mulsi3, ___mulsi3
__mulsi3:
___mulsi3:
	move.l	4(%sp),%d0
	move.l	8(%sp),%d1
	bra.s	mul32

| udiv32: d0 = d0 / d1 exactly (d1 != 0); preserves d1 and d2-d7/a0-a6
udiv32:
	movem.l	%d1-%d4,-(%sp)
	cmp.l	#0xffff,%d1
	bhi.s	3f
| 16-bit divisor: two divu steps
	move.l	%d0,%d2
	clr.w	%d2
	swap	%d2				| n.hi
	divu.w	%d1,%d2			| r1:q.hi
	move.l	%d2,%d3
	move.w	%d0,%d2			| r1:n.lo
	divu.w	%d1,%d2			| r:q.lo
	move.w	%d3,%d0
	swap	%d0
	move.w	%d2,%d0
	bra.s	9f
3:	tst.l	%d1
	bpl.s	5f
	cmp.l	%d1,%d0			| divisor >= 2^31: quotient 0 or 1
	scc	%d0
	neg.b	%d0
	and.l	#1,%d0
	bra.s	9f
5:	moveq	#0,%d2			| restoring shift-subtract
	moveq	#31,%d3
6:	add.l	%d0,%d0
	addx.l	%d2,%d2
	cmp.l	%d1,%d2
	bcs.s	7f
	sub.l	%d1,%d2
	addq.l	#1,%d0
7:	dbra	%d3,6b
9:	movem.l	(%sp)+,%d1-%d4
	rts

| jdiv: jagonly.c's __udivsi3 -- d0 = d0 / d1, saturating; preserves d1
jdiv:
	move.l	%d2,-(%sp)
	move.l	%d0,%d2
	rol.l	#2,%d2
	and.l	#3,%d2			| a >> 30
	cmp.l	%d1,%d2
	bcs.s	1f
	move.l	#0x7fffffff,%d0
	move.l	(%sp)+,%d2
	rts
1:	move.l	(%sp)+,%d2
	bra.s	udiv32

	.globl	__udivsi3, ___udivsi3
__udivsi3:
___udivsi3:
	move.l	4(%sp),%d0
	move.l	8(%sp),%d1
	bra.s	jdiv

	.globl	__divsi3, ___divsi3
__divsi3:
___divsi3:
	move.l	%d2,-(%sp)
	move.l	8(%sp),%d0
	move.l	12(%sp),%d1
	move.l	%d0,%d2
	eor.l	%d1,%d2			| sign of the result
	tst.l	%d0
	bpl.s	1f
	neg.l	%d0
1:	tst.l	%d1
	bpl.s	2f
	neg.l	%d1
2:	bsr.s	jdiv
	tst.l	%d2
	bpl.s	3f
	neg.l	%d0
3:	move.l	(%sp)+,%d2
	rts

	.globl	__umodsi3, ___umodsi3, __modsi3, ___modsi3
__umodsi3:
___umodsi3:
__modsi3:
___modsi3:
	move.l	4(%sp),%d0
	move.l	8(%sp),%d1
	bsr.s	jdiv			| d0 = q, d1 = b
	bsr	mul32			| d0 = q*b
	neg.l	%d0
	add.l	4(%sp),%d0		| a - q*b
	rts

| void *memcpy(void *dst, const void *src, size_t n)
	.globl	memcpy, _memcpy, memmove, _memmove
memcpy:
_memcpy:
memmove:
_memmove:
	move.l	4(%sp),%a0
	move.l	8(%sp),%a1
	move.l	12(%sp),%d1
	move.l	%a0,%d0
	cmp.l	%a1,%a0
	bhi.s	3f				| dst above src: copy backwards
	bra.s	2f
1:	move.b	(%a1)+,(%a0)+
2:	subq.l	#1,%d1
	bcc.s	1b
	rts
3:	add.l	%d1,%a0
	add.l	%d1,%a1
	bra.s	5f
4:	move.b	-(%a1),-(%a0)
5:	subq.l	#1,%d1
	bcc.s	4b
	rts

| void *memset(void *dst, int c, size_t n)
	.globl	memset, _memset
memset:
_memset:
	move.l	4(%sp),%a0
	move.l	8(%sp),%d0
	move.l	12(%sp),%d1
	move.l	%a0,%a1
	bra.s	2f
1:	move.b	%d0,(%a0)+
2:	subq.l	#1,%d1
	bcc.s	1b
	move.l	%a1,%d0
	rts

	.section .note.GNU-stack,"",@progbits
