#!/bin/sh
#
# verify.sh - static verification for Jaguar Doom Ultimate
#
# This cannot run the game (that needs real hardware or an emulator plus a
# jagdoom.wad dumped from your own cartridge).  What it does check:
#
#   1. every GPU/DSP/68k .gas file assembles with rmac (Madmac-compatible)
#   2. every C file passes a syntax/type check with an m68k gcc
#   3. every GPU and DSP overlay fits its local-RAM budget, counting the
#      scratch arrays that live past `codeend` and leaving room for the stack
#
# Usage:  tools/verify.sh            (run from anywhere)
#
# Environment:
#   RMAC=/path/to/rmac               (default: tools/bin/rmac, downloaded if missing)
#   M68KCC=m68k-linux-gnu-gcc        (any m68k gcc; skipped if not found)

set -u
cd "$(dirname "$0")/.."
ROOT=$(pwd)
OUT="$ROOT/build/verify"
mkdir -p "$OUT"
fail=0

RMAC=${RMAC:-$ROOT/tools/bin/rmac}
if [ ! -x "$RMAC" ]; then
	echo "rmac not found, fetching rmac 2.2.37 (linux x64)..."
	mkdir -p "$ROOT/tools/bin"
	( cd "$ROOT/tools/bin" &&
	  curl -sSfLO https://rmac.is-slick.com/static/rmac-2.2.37-lin-x64.zip &&
	  unzip -o -q rmac-2.2.37-lin-x64.zip && chmod +x rmac ) || {
		echo "could not fetch rmac; set RMAC=/path/to/rmac"; exit 2; }
fi

echo "== assembling (.gas -> a.out) with $RMAC"
for f in *.gas; do
	o="$OUT/${f%.gas}.o"
	if ! "$RMAC" -fb "$f" -o "$o" > "$OUT/${f%.gas}.log" 2>&1 ||
	   grep -qi "error\|warning" "$OUT/${f%.gas}.log"; then
		echo "FAIL  $f"; sed 's/^/      /' "$OUT/${f%.gas}.log"; fail=1
	fi
done

CC=${M68KCC:-m68k-linux-gnu-gcc}
if command -v "$CC" > /dev/null 2>&1; then
	echo "== C syntax check with $CC"
	for f in *.c; do
		if ! "$CC" -m68000 -DJAGUAR -std=gnu89 -fsyntax-only -w "$f" > "$OUT/${f%.c}.clog" 2>&1; then
			echo "FAIL  $f"; sed 's/^/      /' "$OUT/${f%.c}.clog"; fail=1
		fi
	done
else
	echo "== ($CC not found, skipping C check)"
fi

echo "== local RAM budgets"
python3 "$ROOT/tools/budget.py" "$OUT" || fail=1

if [ $fail -ne 0 ]; then
	echo "VERIFY FAILED"; exit 1
fi
echo "VERIFY OK"
