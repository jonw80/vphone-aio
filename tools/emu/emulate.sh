#!/bin/sh
#
# emulate.sh - run a cartridge image in Virtual Jaguar (GPL, libretro core),
# headless, saving screenshots and a dump of the 2 MB RAM.
#
#   tools/emu/emulate.sh doom-ultimate.j64 [frames] [every]
#
# Builds the core and the tiny frontend (run.c) into build/emu on first use.
# Screenshots: build/emu/shots/fNNNNN.png, RAM: build/emu/shots/fram.bin.
# Read game state from the RAM dump with the symbols in build/doom.elf.
set -eu
cd "$(dirname "$0")/../.."
ROM=$(realpath "$1"); FRAMES=${2:-1800}; EVERY=${3:-300}
W=build/emu
mkdir -p "$W/sys" "$W/shots"
if [ ! -f "$W/virtualjaguar-libretro/virtualjaguar_libretro.so" ]; then
	[ -d "$W/virtualjaguar-libretro" ] ||
		git clone -q --depth 1 https://github.com/libretro/virtualjaguar-libretro.git "$W/virtualjaguar-libretro"
	make -s -C "$W/virtualjaguar-libretro" -j"$(nproc)"
fi
[ -x "$W/run" ] || cc -O2 -std=gnu11 -w -I"$W/virtualjaguar-libretro/libretro-common/include" tools/emu/run.c -o "$W/run" -ldl
"$W/run" "$W/virtualjaguar-libretro/virtualjaguar_libretro.so" "$ROM" "$W/sys" "$FRAMES" "$W/shots/f" "$EVERY" 2>/dev/null
python3 - "$W/shots" <<'PY'
import glob, os, sys
from PIL import Image
for f in sorted(glob.glob(os.path.join(sys.argv[1], "f*.ppm"))):
    im = Image.open(f)
    im.resize((im.width * 2, im.height * 2), Image.NEAREST).save(f[:-4] + ".png")
    os.remove(f)
print("screenshots in", sys.argv[1])
PY
