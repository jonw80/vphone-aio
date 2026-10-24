#!/bin/sh
#
# build-tc.sh - a complete cartridge image from the "Jaguar Doom TC" PWAD.
#
#   tools/build-tc.sh jaguartc.wad freedoom1.wad [doom-tc.j64] [sprites.wad]
#
# The TC carries the Jaguar levels, textures and status bar for PC ports and
# borrows everything else from DOOM.WAD; this takes those parts from the
# second WAD instead (Freedoom's freedoom1.wad keeps the image free of id
# data beyond what the TC itself holds).  Builds the program if needed,
# converts the WADs (tools/mkwad.py) with the two attract-mode demos
# recorded for these levels (tools/demos/, made with tools/emu/autoplay.py
# --record), restores spectres to episodes 2 and 3, and writes the .j64.
# A fourth WAD, such as your own DOOM.WAD, supplies the sprites instead of
# the second (the image then holds id Software's art: keep it to yourself).
set -eu
[ $# -ge 2 ] || { sed -n '3,15p' "$0"; exit 2; }
PWAD=$(realpath "$1"); IWAD=$(realpath "$2"); OUT=$(realpath -m "${3:-doom-tc.j64}")
SPRITES=; [ $# -lt 4 ] || SPRITES=$(realpath "$4")
cd "$(dirname "$0")/.."
[ -f build/doom.bin ] || python3 tools/build.py
python3 tools/mkwad.py --pwad "$PWAD" --iwad "$IWAD" -o build/jagdoom-tc.wad --spectres \
	--demo1 tools/demos/tc-demo1.lmp --demo2 tools/demos/tc-demo2.lmp ${SPRITES:+--sprites "$SPRITES"}
python3 tools/mkrom.py --wad build/jagdoom-tc.wad -o "$OUT"
