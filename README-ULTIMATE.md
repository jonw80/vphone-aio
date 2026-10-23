# Jaguar Doom: Ultimate

A set of hardware-level upgrades to the released Jaguar Doom source, each one
grounded in the *Jaguar Technical Reference Manual* (Tom & Jerry, rev. 8,
28 Feb 2001), which this document cites as "TRM p.N".

| Change | What you get | Where |
|---|---|---|
| [In-game music](#1-in-game-music) | The cartridge's ten songs play during levels, mixed with sound effects. New **Music** volume slider. | `mixmusic.gas`, `s_sound.c`, `g_game.c`, `o_main.c` |
| [Translucent spectres](#2-translucent-spectres) | Spectres shimmer as a darkened, displaced copy of what's behind them, like PC Doom's fuzz. They used to be drawn as ordinary opaque demons. | `r_phase3.gas`, `r_phase8.gas` |
| [Faster columns](#3-faster-wall-sky-and-sprite-columns) | Walls, sky and sprites are drawn with one blitter inner loop per column instead of one outer-loop pass per pixel. | `colblit.inc`, `r_phase6.gas`, `r_phase8.gas` |
| [No bus stealing while waiting](#4-no-bus-stealing-while-waiting) | The GPU stops hammering DRAM while it waits for the blitter or the next frame, so the blitter and the 68000 (game logic) get those cycles. | `r_phase7.gas`, `r_phase9.gas` |
| [Verification tooling](#verification) | `tools/verify.sh`: assembles everything, checks the C, checks every GPU/DSP overlay against its RAM budget, and runs functional tests on a JRISC + blitter model. | `tools/`, `tests/` |

**Status: builds with free tools into a cartridge image, verified on models
and a 68000 emulator, not yet run on a Jaguar or a full Jaguar emulator.**
Making the playable `.j64` needs a dump of your own cartridge, because the
game data is not included. See [Building](#building) and
[Testing on hardware](#testing-on-hardware).

---

## 1. In-game music

**The problem.** Jaguar Doom only played music on the title, intermission and
finale screens. Sound came from two separate DSP programs: `sfx.gas` mixes
sound effects, `music.gas` sequences music from sampled instruments in ROM.
Both write the same 22 kHz output ring at `$1F0000` with plain stores, so
whichever ran last erased the other, and `S_UpdateSounds` ran the music job
*synchronously* (the 68000 waited for it). So music and gameplay were
mutually exclusive.

**The fix.** `mixmusic.gas` is one DSP job that runs the sound-effect mixer
and then paints the music into **exactly the ring window the effects mixer
just wrote**, adding with saturation instead of overwriting:

* The effects mixer mixes quads `[start, finalquad)`, where `finalquad =
  samplecount/8 + 512` and a quad is 8 output samples. The ring is 4096
  samples, so music time `T` lands in slot `T mod 4096`; painting music over
  `[start*8 + 4096, finalquad*8 + 4096)` hits the same slots one full ring
  ahead. That is the same latency the shipped music path used
  (`samplecount + 8192`), so song timing is unchanged.
* The job keeps the song clock in step with the effects window itself: a new
  song starts at the window, and if the clock has fallen behind (music volume
  was 0, or a long stall) it jumps forward and delays pending events by the
  same amount.
* It runs asynchronously, like the effects job. `S_StartSong`/`S_StopSong`
  now wait for the DSP to be idle before touching song memory.
* The music write-out does an external `loadw` before every external
  `storew`, which also satisfies the JERRY bug #3 work-around (TRM p.141).

Other changes that fell out of this:

* **The music volume setting finally does something.** The sequencer scaled
  note velocity by `sfxvolume`; the saved `musicvolume` was never read. It is
  now, and the options menu has a **Music** slider (saved to EEPROM as
  before). At music volume 0 the cheaper effects-only job runs.
* Levels start a song in `G_DoLoadLevel`, cycling the cartridge's ten tracks
  by map (Carl Forhan had left a commented-out attempt at exactly this spot).
* Songs are played straight out of cartridge ROM when the lump isn't
  compressed, the way the instruments always were, so in-game music takes no
  zone memory from the level or the texture cache. A compressed song is
  still unpacked into RAM as before.
* Both mixers now fit in DSP RAM together: the effects buffer moved from
  `$F1B800` to `$F1C400` and shrank from 256 to 128 quads (the mixer already
  worked in chunks). Layout: code `$F1B140-$F1C0F4`, effects buffer
  `$F1C400-$F1CC00`, stack below `$F1CE00`.
* **Latent bug fixed:** the shipped effects mixer placed a sound that starts
  part-way through a mixing window at 32 bytes per quad, in a buffer laid out
  at 16 bytes per quad. In practice sounds start at a window boundary, which
  is why it never showed.

**Cost.** On the model the combined job executes ~87k DSP instructions per
15 Hz tic, versus ~23k for effects alone (music is ~59k). The DSP also runs
the game's movement/sight/shooting code (`p_*.gas`) through synchronous
`DSPFunction` calls, so while music is on, a physics call can occasionally
wait for the mixer to finish; and the instrument reads come from cartridge
ROM over the bus. That trade-off is why the music slider exists: slide it to
0 and you are back to the original DSP load.

## 2. Translucent spectres

**The problem.** The Jaguar renderer never tests `MF_SHADOW`, so spectres
(`MT_SHADOWS`) were drawn as ordinary pink demons. (The blur-sphere
invisibility power-up was cut from the port entirely.)

**The fix.** `R_PrepMobj` (`r_phase3.gas`) tags `MF_SHADOW` things with
colormap `-1`. `R_DrawVisSprite` (`r_phase8.gas`) then points the blitter's
A1 **source at the screen being drawn** (160-wide window, +1 row per pixel)
instead of at the patch, and `SRCSHADE` darkens what it reads by 40
intensity steps (TRM p.75: "may be used to lighten or darken images"). The
destination is the normal A2 screen pointer.

* A Doom patch column is a list of *posts*: runs of opaque pixels. Blitting
  per post means the post list is the transparency mask; no extra pass.
* The source row is 1 or 2 rows below the destination, alternating in pairs
  of columns and crawling with `framecount`, so the spectre shimmers. Rows
  below haven't been overwritten yet (the blit runs downward), so there's no
  smearing; near the bottom of the view the offset falls back to 0.
* Cost: identical to drawing a normal sprite column.

## 3. Faster wall, sky and sprite columns

**The problem.** The shipped column drawers blit each column with
`count = (pixels << 16) | 1`: every pixel is one pass of the blitter's
**outer** loop, so that the texture pointer can advance by a 16.16 step
using the outer-loop step registers. Each pass pays for three pointer
updates, `UPDA1F`, `UPDA1` and `UPDA2`, which the manual says cost "a
one-tick overhead per update" (TRM p.73), plus the loop turnaround.

**The fix** (`colblit.inc`, `FASTCOLUMNS = 1`). Each column is one **inner**
loop, `count = (1 << 16) | pixels`, with no outer-loop updates:

* A1 (the texture, the *source* because of `DSTA2`) uses **X add =
  increment** with the 16.16 step in `A1_INC`/`A1_FINC`. This is the DDA
  mode the manual's image-rotation example uses for a scaled source with
  `DSTA2` (TRM pp.71, 79).
* A2 (the screen) uses X add = zero and **Y add = 1**, moving down one line
  per pixel.
* TRM bug #11 says the A1 and A2 Y add bits are not decoded separately and
  gives the work-around: set it on both. A1 ignores its Y add bit in
  add-increment mode (TRM p.71), so that costs nothing.
* After the sprite phase the address flags are restored exactly as the
  shipped code left them, so the 68000's own blits (status bar, automap,
  menus) see what they always saw.

`FASTCOLUMNS = 0` reassembles the shipped blits bit for bit (the tests check
that the constants match: `$23820`, `$23A20`, `$41802F01`).

This is the one change that rests on hardware behaviour the manual only
describes in prose (the bug #11 wording), so it is the first thing to check
on hardware; see below.

## 4. No bus stealing while waiting

TRM p.8 lists the bus priorities: **GPU at normal priority outranks the
blitter at normal priority and the 68000.**

* `r_phase7.gas` (floors/ceilings) waited for the blitter by polling its
  status register, but the compiled code also **stored every sample to a
  variable in DRAM** (`_junk`). Each of those stores took the bus away from
  the blit it was waiting for. The poll now reads only the status register,
  which sits on the GPU's local bus and costs no main-bus cycles.
* `r_phase9.gas` spins at the end of each frame for the 30 fps frame cap and
  for the vblank ISR to take the new object list, re-reading `ticcount`,
  `lastticcount` and `displaylist_p` from DRAM and storing to `_junk` on
  every iteration. Meanwhile the 68000 is running the *next* game tic, at
  the lowest bus priority. The constants now stay in registers, and each
  poll is followed by a 64-iteration local-RAM delay (~12 us) so the 68000
  gets the bus back. Worst-case added latency is far below one scanline.

---

## Verification

```sh
tools/verify.sh
```

1. **Assembly.** Every `.gas` file assembles with
   [rmac](https://rmac.is-slick.com/) 2.2.37 (Madmac-compatible; downloaded
   into `tools/bin/` on first run) with no errors or warnings.
2. **C.** Every `.c` file passes `m68k-linux-gnu-gcc -m68000 -DJAGUAR
   -fsyntax-only`. Two behaviour-identical edits to `jagonly.c` were needed
   because GCC 2.6 accepted a cast-as-lvalue and a `volatile` mismatch.
3. **RAM budgets** (`tools/budget.py`). Every GPU overlay (loaded at
   `$F03100`, stack from `$F04000`) and DSP job (`$F1B140`, stack from
   `$F1CE00`) must fit, including scratch arrays that live past `codeend`
   (`clipbounds`, `spanstart`, `solidsegs`), with stack headroom calibrated
   against the shipped `p_base.gas`.
4. **Functional tests** on `tools/jrisc.py` (a JRISC GPU/DSP model written
   from TRM pp.44-58 and 100-110) and `tools/blitter.py` (a blitter model
   written from TRM pp.64-82):
   * `test_mixmusic.py` uses the **shipped** `sfx.gas` and `music.gas` as the
     oracle: over six consecutive tics (the whole 4096-slot ring, wrapping)
     `mixmusic` must produce `saturate16(sfx + music)` sample for sample,
     leave every other slot alone, and end with identical effects and
     sequencer state. Also: the mid-window sound fix against a Python model
     of the mixing arithmetic (and a check that the shipped mixer fails it),
     clock resync, no stray DSP RAM writes, stack use (104 bytes).
   * `test_columns.py` runs the real `R_DrawTexture` and `R_DrawVisSprite`:
     with `FASTCOLUMNS` on and off they must draw **identical pixels** for
     walls and sprites under **both** readings of bug #11 (A2's Y add bit
     honoured, or driven by A1's). Spectres must be darkened copies of the
     screen 0-2 rows below and touch nothing else. The sky blit, which is
     inlined in `R_SegLoop`, is checked statically for the registers it
     programs.

The tests have already paid for themselves: they caught that rmac evaluates
expressions **strictly left to right** (`1+2*3` is 9), so an address written
as `$f02200+4*N` pointed at garbage. `colblit.inc` now defines register
addresses outright and warns about this.

**Limits of the models.** They are functional: in-order execution with the
jump delay slot, no timing, no bus contention, no interrupts, and none of the
hardware bugs except the bug #11 variants. They prove the code does what the
manual says it should; they cannot prove the silicon agrees.

## Building

### With free tools (tested)

```sh
tools/build.py                                     # -> build/doom.bin, doom.elf, doom.json
tools/mkrom.py --dump your-jaguar-doom.j64 -o doom-ultimate.j64
#   or:  tools/mkrom.py --wad jagdoom.wad -o doom-ultimate.j64
```

`build.py` needs rmac (fetched by `verify.sh`) and an m68k gcc
(`m68k-linux-gnu-gcc`); `mkrom.py` needs only Python and `build/`.

**The game data is not included.** It is id Software's: bring a dump of
your own cartridge (4 MB, `$800000-$BFFFFF`) or the WAD from it
(`$840000` to the end). `mkrom.py` checks it really is the Jaguar Doom WAD,
accepts word-swapped dumps, keeps the dump's boot header and the WAD
byte-for-byte, and replaces only `$802000-$83FFFF` with a 44-byte loader
(`romstub.S`: copy the program to `$4000`, clear its BSS, jump) followed by
`doom.bin`. The program is 241,568 bytes, leaving about 12 KB free.

What the free-tool build had to deal with, all handled by `build.py`:

* **GPU/DSP `movei` relocations.** `movei` stores its immediate low word
  first; GNU `ld` patches big-endian, and rmac's ELF writer also stores the
  addend word-swapped. `build.py` reads which relocations are `movei`s from
  rmac's a.out output (bit 0 of `r_info`, the flag Atari's `aln` used) and
  fixes all 487 of them after linking. `tools/check_image.py` then
  re-assembles every GPU/DSP block at its linked addresses and requires the
  image to match byte-for-byte (all 21 do).
* **Modern GCC.** Variables written by the GPU, DSP or vblank interrupt and
  polled by the 68000 are now `volatile` (GCC 2.6 happened to reload them).
  The game's own 32-bit multiply/divide helpers in `jagonly.c` compile, under
  a modern GCC, into calls to themselves, so with `MODERN_GCC` they come from
  `lib68k.S` instead, with the same semantics including the original's
  quirks (verified on 200,256 cases under QEMU against a transcription of the
  C). The distribution libgcc is not used: it is built for the 68020.
* **`init.s`** needed `jaguar.inc`, which was not released; `jagregs.inc`
  supplies the registers it uses, from the manual. Its one `.data` variable
  moved to `.bss`, because rmac's ELF writer drops 68000 `.data` sections
  (`build.py` now refuses any relocation that lost its target).

`tests/smoke_boot.py` boots the resulting cartridge's 68000 code on
Unicorn's 68000 model, with a stand-in WAD and the GPU/DSP handshakes
stubbed: the loader copies the program and clears BSS, `init.s` runs and
takes vblank interrupts, and the game runs through the EEPROM, zone and WAD
code until it stops exactly where a stand-in WAD must, with
`I_Error: "W_GetNumForName: TEXTURE1 not found!"`. It cannot show the GPU
and DSP code running on the real chips.

**In an emulator.** `tools/emu/emulate.sh doom-ultimate.j64` builds Virtual
Jaguar's libretro core (GPL) and a small headless frontend, runs the image,
and saves screenshots plus a RAM dump. With the stand-in WAD the emulated
Jaguar shows the game's own start-up log (`GPU_main`, `C_Init` ... 
`R_InitData`) and then the expected `TEXTURE1 not found` error; with your
real dump it runs the game.

**Booting on a console.** The Jaguar boot ROM only starts a cartridge whose
header carries an Atari-encrypted boot block, and a commercial header can
verify the contents it was made for, so a modified program may not boot on
a stock console with your cartridge's original header. The image is meant
for emulators and for flash carts that load `.j64` files themselves;
`mkrom.py --header` takes a different 8 KB header if you have one.

### With the original Atari toolchain

The original `Makefile` and `mydoom.lnk` (Atari `mac`/`aln` and the Jaguar
GCC 2.6 port) are updated: `mixmusic.o` replaces `music.o`, and
`r_phase6.o`/`r_phase8.o` depend on `colblit.inc`. `music.gas` is kept for
reference but no longer linked. Load addresses are unchanged: `doom.abs` at
`$4000`, `jagdoom.wad` at `$840000`.

## Testing on hardware

What to look at, in order of risk:

1. **Walls, sky and sprites render normally** (`FASTCOLUMNS`). If columns
   smear, stretch or show garbage, set `FASTCOLUMNS .equ 0` in
   `colblit.inc` and rebuild; that restores the shipped blits exactly and
   isolates the problem to bug #11 behaviour.
2. **Music plays in levels and mixes with effects**; the Music slider
   changes its volume and 0 silences it; music changes cleanly at the
   intermission and on the next level; no clicks at song changes.
3. **Spectres** (thing type 58, wherever a map places them) shimmer and are
   translucent instead of looking like pink demons.
4. **Frame rate.** Set `debugscreenstate = true` in `jagonly.c` to show `vblsinframe` (vblanks per frame, 4-8) and the
   refresh timing in `R_DebugScreen`. Compare against the shipped build in
   the same spot: expect gains in wall- and sprite-heavy views from
   `FASTCOLUMNS` and the bus fixes, and some cost with music on.

## Looked at and not done

* **320-wide rendering.** The obvious visual upgrade, but the 160-column
  assumption is baked into precomputed tables (`xtoviewangle`, `distscale`,
  `viewangletox`), hand-computed structure offsets in the GPU code (a
  visplane is 348 bytes because it holds 160 columns), and every column
  loop. It also doubles per-column GPU work, which is the refresh's main
  cost. Doing it well is a rewrite of the refresh, not a patch.
* **Loading GPU overlays with the blitter** (the manual's recommended method,
  TRM p.36). The resident `gpubase` has 16 bytes free, the phases rely on
  blitter state set by `R_Setup` that the loader would clobber, and the
  estimated saving is about 1 ms per frame.
* **Deeper light diminishing.** Jaguar lighting subtracts at most
  `(255 - light)/4` from CRY intensity, which is flatter than PC Doom's
  colormaps. Steeper is easy (one shift), but subtractive shading crushes
  dark texels to black instead of scaling them, and it is a matter of taste
  that needs eyes on a screen.

## Licence

The Jaguar Doom source is covered by id Software's limited-use licence in
`license.txt` (non-commercial, educational use). These changes don't alter
that; don't distribute builds for money.
