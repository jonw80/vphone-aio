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
| [Detail: 160, 320 or 640 columns](#6-detail-160-320-or-640-columns) | A **Detail** line in Options. Low is the original 160-column view; High draws 320 columns, one per pixel of the Jaguar's 320-pixel mode; Ultra draws 640 for emulators on a PC. | `r_detail.c`, `r_phase1`-`9.gas`, `jagonly.c`, `gpubase.gas` |
| [Triple buffering, and a 68000 that sleeps](#8-triple-buffering-and-a-68000-that-sleeps) | The GPU goes straight on to the next frame instead of waiting for the vblank to take the last one, and the 68000 stops (STOP) instead of polling while it waits. On the attract demos High runs 12-20% faster, Low comes within 2% of the 30 fps cap. | `r_phase9.gas`, `init.s`, `jagonly.c`, `gpubase.gas`, `r_phase3.gas` |
| [Textures tile down tall walls](#7-textures-tile-down-tall-walls) | Walls taller than their texture repeat it, as on the PC, instead of running into the next texture column and then into whatever memory follows the texture. | `r_phase6.gas` |
| [A complete game from the Jaguar Doom TC](#5-a-complete-game-from-the-jaguar-doom-tc) | `tools/build-tc.sh` turns the PC "Jaguar Doom TC" PWAD plus Freedoom into a full cartridge: all 24 levels, sprites, textures, sounds, music with synthesized instruments, attract-mode demos. Runs in Virtual Jaguar. | `tools/mkwad.py`, `tools/jagmusic.py`, `tools/emu/` |
| [Verification tooling](#verification) | `tools/verify.sh`: assembles everything, checks the C, checks every GPU/DSP overlay against its RAM budget, and runs functional tests on a JRISC + blitter model. | `tools/`, `tests/` |

**Status: builds with free tools into a cartridge image that runs in a
full Jaguar emulator (Virtual Jaguar): title, menus, options, levels from
all three episodes at all three Detail settings, music and effects,
spectres, intermission, attract demos. Not yet run on a real Jaguar.** The game data is not included: bring
a dump of your own cartridge, or build the TC version from the Jaguar Doom
TC PWAD and Freedoom. See [Building](#building) and
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
instead of at the patch, and `SRCSHADE` darkens what it reads by 20
intensity steps (TRM p.75: "may be used to lighten or darken images"). The
destination is the normal A2 screen pointer. SRCSHADE subtracts rather than
scales, so the step is kept small: at 40, spectres in dark rooms came out as
black silhouettes in the emulator; at 20 the room shows through them.

* A Doom patch column is a list of *posts*: runs of opaque pixels. Blitting
  per post means the post list is the transparency mask; no extra pass.
* The source row is 1 or 2 rows below the destination, alternating in pairs
  of columns and crawling with `framecount`, so the spectre shimmers. Rows
  below haven't been overwritten yet (the blit runs downward), so there's no
  smearing; near the bottom of the view the offset falls back to 0.
* Cost: identical to drawing a normal sprite column.

The shipped levels place no spectres at all (the port could not draw them).
`tools/mkwad.py --spectres` puts them back: every second demon on MAP09 and
later becomes one.

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
* `r_phase9.gas` spun at the end of each frame for the 30 fps frame cap and
  for the vblank ISR to take the new object list, re-reading `ticcount`,
  `lastticcount` and `displaylist_p` from DRAM and storing to `_junk` on
  every iteration. Meanwhile the 68000 is running the *next* game tic, at
  the lowest bus priority. The constants now stay in registers, and each
  poll is followed by a 64-iteration local-RAM delay (~12 us) so the 68000
  gets the bus back. Worst-case added latency is far below one scanline.
  (Since [section 8](#8-triple-buffering-and-a-68000-that-sleeps) the only
  wait left there is for a frame still queued from before, polled the same
  way, and the 68000 sleeps instead of polling while it waits for the GPU.)

---

## 5. A complete game from the Jaguar Doom TC

The Jaguar Doom TC (`jaguartc.wad`) is a PC-port PWAD that
recreates the Jaguar levels (with their Jaguar order, textures, status bar
and intermission text) and borrows sprites, sounds, music and most textures
from DOOM.WAD. `tools/build-tc.sh jaguartc.wad freedoom1.wad doom-tc.j64`
turns it back into a Jaguar cartridge, taking everything it borrows from
[Freedoom](https://freedoom.github.io/) (BSD licence) instead:

```sh
tools/build-tc.sh jaguartc.wad freedoom1.wad doom-tc.j64     # about 3 seconds
tools/emu/emulate.sh doom-tc.j64                             # or any Jaguar emulator
```

If you own Doom, a fourth argument takes the sprites from your DOOM.WAD
instead of Freedoom's (everything else still comes from Freedoom):
`tools/build-tc.sh jaguartc.wad freedoom1.wad doom-tc.j64 doom.wad`. The
two share palette 0, so nothing else changes. That image holds id
Software's art, so keep it for yourself. With the Ultimate Doom sprites
the WAD is 100 KB smaller (406 KB left in the cartridge, against 304 KB).

`tools/mkwad.py` writes the WAD the way the engine reads it, all of it taken
from the loaders and the GPU code rather than guessed:

* **Sprites** at the exact lump numbers `sprinfo.c` was generated with
  (0-1217): an uncompressed patch header + post list read in place from ROM
  (posts long-aligned, since the GPU reads them as longs), and the opaque
  pixels LZSS-packed in the next lump. Rotations and mirrored views follow
  each frame's flip flags. Weapon sprites are halved in width and their
  offsets moved: `R_FinishPSprite` draws them 1:1 on the 160x180 view,
  whose pixels are twice as wide as the PC's (2x or 4x across on the
  wider views of [section 6](#6-detail-160-320-or-640-columns)).
* **Textures** (71, of the 128 the engine holds) composited from patches,
  column-major, power-of-2 wide; the TC's own artwork where it has it,
  Freedoom's definitions otherwise. One texture shares its name with a
  sound (`METAL`); since texture pixels are found by a backwards name
  search, it is renamed in the level data.
* **Flats** with the `NUKAGE`/`FWATER`/`LAVA` cycles kept contiguous.
* **Levels**: the TC's maps renumbered MAP01-MAP24 by its MAPINFO
  `levelnum`s (E1M1-E1M8, E2M1-E2M8, E3M1, E3M3-E3M8, E1M9).
* **Graphics** as jagobjs: title and credits, the 4-bit `M_TITLE` menu
  background, menu labels and both fonts drawn from Freedoom's big font in
  red, status bar (the TC's Jaguar bar), faces, digits padded to a common
  cell (`ST_Drawer` erases old numbers using the minus sign's height),
  intermission labels, plaques.
* **Sounds** resampled to 11025 Hz. Sounds no code path plays (cyberdemon
  and spider mastermind, which the port lacks) are short stubs, to fit.
* **Music** (`tools/jagmusic.py`): Freedoom's MIDI songs converted to the
  DSP sequencer's event stream (10 voices, voice stealing, volume,
  expression, pitch bend, sustain pedal). Instruments are not in a MIDI
  file, so they are synthesized from the IWAD's `GENMIDI` OPL2 patches
  with a two-operator FM model: envelope baked into the sample, loop over
  the steady part for sustaining patches, base pitch chosen so the loop
  is a whole number of periods.
* **LZSS** with a greedy longest-match search; `tests/test_lzss.py` runs the
  game's own decoders (`decomp.gas` for `W_ReadLump`, `R_decode` in
  `r_phase5.gas` for pixels) on its output in the JRISC model.

The image is 3.63 MB of WAD with 300 KB to spare in the 4 MB cartridge.

**Played in the emulator.** `tools/emu/run.c` has a control mode (buttons,
RAM peek/poke, screenshots, WAV capture) and `tools/emu/drive.py` drives it
from Python. `tools/emu/autoplay.py` plays a level by itself: it plans a
route to the exit on the level geometry (blocking lines, steps, doors,
solid things, collision boxes the way `P_CheckPosition` sees them), steers
by reading the player's position from emulated RAM, fights what it sees
and presses the exit switch. It finished E1M1 into the intermission, and
gave the screenshots of every episode, spectres and the options screen.
`tools/emu/detail.py` starts every level at each Detail setting and
measures the frame rate ([section 6](#6-detail-160-320-or-640-columns)).
`tools/emu/profile.py` breaks a frame down into the GPU's refresh phases
and the 68000's tic, and `tools/emu/demobench.py` times the attract demos,
the same views on every build
([section 8](#8-triple-buffering-and-a-68000-that-sleeps)).
`tools/emu/endings.py` plays the episode endings: on E1M8 it rides the lift
into the star, opens the cage and kills both barons, and `A_BossDeath`
lowers the tag-666 floor (176 to -136, read from RAM) a second after the
second one dies; on E3M8 it opens the yellow door, crosses the exit, and
the intermission, end text and cast call follow (the cast is drawn by the
68000's `BufferedDrawSprite`, a second check of the sprite format). The
Jaguar port has no Cyberdemon or Spider Mastermind: E2M8 and E3M8 end
with barons and cacodemons instead.
The two attract-mode demos in `tools/demos/` were recorded by it with the
game's own demo recorder (`demorecording` set during the level load, the
buffer in the free 7.9 KB after the first frame buffer), and replay
exactly: playback ends at the position where the recording ended.

**Found on the way.** `S_Init` started its instrument loop on the
`INSTSTRT` marker itself and stored that lump's "instrument number" (3386)
far past the end of `instruments[256]`; it now starts after the marker and
bounds-checks.

## 6. Detail: 160, 320 or 640 columns

**The problem.** The shipped refresh draws a 160x180 view and shows it in a
video mode whose pixels are 8 clocks wide (VMODE PWIDTH 8, TRM p.68), so
each column is two pixels of the 320-pixel status bar beneath it. The width
was not a setting: it was in the precomputed angle tables, in structure
sizes the GPU code had worked out by hand (a visplane was 348 bytes because
it held 160 columns), and in constants all through the nine GPU phases.

**What it does now.** Options has a **Detail** line, saved in the EEPROM:

| Detail | Columns | Video mode | For |
|---|---|---|---|
| Low | 160 | PWIDTH 8 | the original |
| High | 320 | PWIDTH 4, the 320-pixel mode the status bar and menus use | the Jaguar's full horizontal resolution |
| Ultra | 640 | PWIDTH 2 for the whole frame | emulators on a PC; more GPU work than the hardware keeps up with |

The view stays 180 lines with the same vertical scale, so the picture has
the same shape and field of view in every mode, with two or four times the
horizontal detail.

**How.**

* `r_detail.c` builds `viewangletox[]`, `xtoviewangle[]` and `distscale[]`
  for the width at run time with the arithmetic the shipped tables were made
  with (a 90 degree field of view, `FixedMul` rounding down). At 160 columns
  it reproduces the shipped tables exactly, so `tables.c` no longer carries
  them. The 68000's 32-bit multiply is a library call, so the table code
  multiplies with `mulu.w` and the column search is linear (one pass over
  the angles); a change of Detail costs a fraction of a second, applied in
  `R_Setup` when the GPU is idle.
* The GPU phases read the width from main RAM instead of constants:
  `screenwidth` for the solid-segment sentinels and the sprite and weapon
  edge clips; `centerxfrac` for the sprite projection; `clipangle` (which
  is no longer exactly the same angle at every width); `centerx` for the
  flat steps; and the blitter's screen window flags (`col_a2flags`,
  `col_a1screen`, `plane_a2flags`, made from the TRM p.80 width codes $1D,
  $21 and $25). Sprites keep their height through `stretchx`, the 2.2
  vertical stretch divided by the width factor. The weapon is drawn 2x or
  4x across.
* Visplanes are laid out for 640 columns in every mode (`VISPLANE_SIZE`,
  1308 bytes). The span commands that `R_PlaneLoop` queues for
  `R_MapPlane` were packed as `x2<<16 | y<<8 | x1`, which caps x at 255;
  they are now `x2<<20 | y<<10 | x1`.
* The per-column arrays `clipbounds[]` (phase 6) and `spropening[]`
  (phase 8) live in GPU RAM, which only has room for 160 of them. The
  phases now go through a pointer that points at the GPU RAM copy at Low,
  so Low keeps its speed, and at a main RAM array when wider.
* The frame buffers grow to 230 KB each (640x180x2). They move down to
  `$17F800` and `$1B7C00`, ending at the sound buffer, and the heap's top
  moves down with them (below Ultra the same memory holds four 320x180
  screens; see [section 8](#8-triple-buffering-and-a-68000-that-sleeps)). That costs the purgeable texture cache 324 KB of its
  800 KB; levels use 70-190 KB of it.
* **Ultra and the status bar.** The shipped game switches the video mode to
  320 pixels at the status bar from the GPU's object interrupt. Real
  hardware takes a mid-frame mode change in its stride, but emulators such
  as Virtual Jaguar size their output from one video mode per frame, and a
  640 top over a 320 bottom shows half the view. So at Ultra the frame stays
  in the 640-pixel mode, and the two status bar objects become *scaled*
  bitmap objects (TRM p.19) with HSCALE 2.0, which write every pixel twice
  into the line buffer. They now have three-phrase slots on 32-byte
  boundaries in every mode (`init.s` aligns the list buffers and
  `CopyObjList` copies 44 longs). Their TYPE is 0, unscaled, below Ultra.
  The mode the interrupt writes is a variable in `gpubase`,
  `_sbarvmode`, which each frame sets as it goes up.
* The automap draws at the view's width, with the same map scale across
  the screen, so finer lines. The pause plaque is drawn 2x or 4x across, and
  is told the 320-pixel front-end page from a 320-column view by a flag
  (`frontendlist`, set by whichever was posted last), since the two use the
  same VMODE. The automap clears its
  view a line at a time: 640x180 pixels would overflow the blitter's 16-bit
  inner count.

**Cost.** Columns are the refresh's main expense, and High has twice as
many. Measured in Virtual Jaguar with its DRAM, blitter and GPU pipeline
timing models switched on (`tools/emu/detail.py`, five seconds turning on
the spot at the start of every level):

| Detail | Mean over 24 levels | Slowest level | Levels at the 30 cap |
|---|---|---|---|
| Low (160) | 29.8 frames/s | 28.4 (MAP02) | 18 |
| High (320) | 28.2 frames/s | 22.6 (MAP21) | 10 |
| Ultra (640) | 19.8 frames/s | 13.4 (MAP21) | 1 |

(`VJ_OPTS="dram_timing=enabled,blitter_timing=enabled,gpu_pipeline_timing=enabled" tools/emu/detail.py doom-tc.j64 outdir`;
`tools/emu/run.c` passes `VJ_OPTS` to the core as its options.)

(This is the build with [section 8](#8-triple-buffering-and-a-68000-that-sleeps)'s
triple buffering. The table this replaced was measured on a build whose
68000 polled GPU RAM, which the emulator's timing model rewards with
nearly free blits; see that section.)

The game caps itself at 30 frames a second, which hides Low's real speed
on most levels. In the slowest level, MAP21, High keeps 79% of Low's frame
rate and Ultra 47%. The emulator's timing is a model ("still being calibrated", its
options say), so read these as the ratio between the modes, not as what a
console will show; on hardware, Ultra is for emulators.

**Checking Low.** Low has to draw exactly what it drew before. Comparing it
with the previous build turned up something unexpected: the previous build
does not draw the same pixels as *itself* once its data moves. Shifting its
`.data` by 4 bytes changes a few texels in about half the frames of the
attract demos. Taken one step at a time in the emulator, with a GPU hook
logging every wall blit at one screen column, it comes down to this: a wall
column taller than its texture ran off the end of the cached texture into
the next zone block's header. That header holds a pointer to the block's
owner, a variable whose address moves with every build. That's the bug
[section 7](#7-textures-tile-down-tall-walls) fixes. With it fixed, the
renderer's output no longer depends on where anything sits in memory: the
current build and the same build shifted by 4 bytes give identical frames
through the whole attract loop (2,204 distinct frames). Before the tiling
fix, the frames where the new Low differed from the previous build differed
in the same few texels, and by the same amount, as the previous build does
from itself shifted by 4 bytes; game state (positions, view height, angle,
tic) was identical throughout.

That comparison needs two test-only changes (not in the source). The game
lets the 68000 run the next tic while the GPU draws, so a build that draws a
little faster shows different tics at a given video frame. One change makes
the next tic wait for the GPU. The other compares the sequence of *distinct*
frames rather than frames at fixed times: demo tics are a fixed four
vblanks of game time and every tic is drawn once, so that sequence doesn't
depend on speed (`tools/emu/run.c`'s `hash` command makes it cheap).

## 7. Textures tile down tall walls

Jaguar Doom caches each wall texture column after column, and draws a wall
column as one linear blit through that data. When a wall was taller than
its texture, the blit simply carried on: into the next column of the
texture, and from the last column into whatever followed the texture in
memory. When the texture position started above the texture, the code
stepped back a whole column instead of wrapping within it. The TC's levels
come from PC ports, where textures repeat down tall walls, so this showed
up as misaligned blocks and stray garbage texels at the tops of wall
details.

`R_DrawTexture` now wraps the texture row at the texture's height, as PC
Doom tiles it, and splits the column's blit where the row wraps: one blit
per repeat, each inside the column. A column that doesn't wrap still costs
one blit, plus a divide to find where it would wrap.
`tests/test_columns.py` checks every pixel of 320 columns against the
texture tiled at heights 128 and 72, many of them wrapping, with a marker
pattern after the texture that must never show. The previous code fails at
the first wrapping column.

## 8. Triple buffering, and a 68000 that sleeps

**The problem.** The shipped frame loop is double buffered. The GPU draws a
frame into the screen that isn't up, then, at the end of `r_phase9.gas`,
waits for the 30 fps cap and for the vblank interrupt to take the new
object list before it reports the frame done. Only then does the 68000
start the next one. `tools/emu/profile.py` put that wait at 10-12 ms of
every frame on MAP21. And a frame that needs 34 ms instead of 33 misses its
vblank: it stays up for three vblanks instead of two, and the next frame
can't even start until it is up.

**What it does now.**

* `r_phase9.gas` *posts* the finished frame: its object list, its VMODE and
  the status bar's, the colour shift, and the screen it is in
  (`pendinglist_p` and the rest in `jagonly.c`). Then it returns. The
  vblank interrupt in `init.s` puts the frame up at the first vblank two or
  more after the last one went up (the same 30 fps cap), copies its list
  and sets `lasttics`, the game time per frame. The GPU only waits if a
  frame is still waiting from before, which means it is a whole frame
  ahead.
* `I_NextScreen` gives each frame a screen that is neither up nor waiting to
  go up. The screen memory, `$17F800` up to the sound buffer at `$1F0000`,
  is four 320x180 slots, so at Low and High the refresh rotates through
  them and never waits for one. A 640x180 screen takes two slots, so Ultra
  stays double buffered; a third Ultra screen would have to come out of the
  texture cache. The automap, the front end's two 8-bit pages, the pause
  and loading plaques and the level loader's scratch buffer all used to
  assume "page 0 or 1". They now ask the same allocator (`FreeScreen`),
  which knows what is up and what is waiting by address range, since a
  640-column screen covers two smaller ones.
* Each frame carries the status bar's video mode with it: the interrupt
  writes `gpubase`'s `_sbarvmode` as the frame goes up. So a Detail change
  can't show the last frame in the old mode with the status bar in the new
  one.

**The 68000 waits with STOP.** The 68000 waits for the GPU twice a frame:
for the refresh to latch the game state, then for it to finish. At Ultra it
also waits for the vblank to free a screen. It used to poll. It is the
lowest-priority bus master (TRM p.8), but it has no cache, so every pass of
a poll loop fetches instructions from DRAM. Each fetch is a bus cycle that
a higher-priority master asking a moment later has to wait out, plus a DRAM
page change for whoever comes next. Now `I_WAITFOR(cond)` (`doomdef.h`)
masks interrupts, tests the condition and executes `STOP #$2000`, which
unmasks them and stops the 68000 in one instruction. An interrupt that
arrives between the test and the STOP still wakes it instead of being
slept through. The vblank wakes it, and so does the GPU, which writes
CPUINT in its control register (TRM p.60, enabled in INT1, p.16) when phase
3 has latched the game state and when it finishes a job (`gpubase.gas`,
which now has no bytes free). The interrupt handler sees a GPU interrupt
on its own, clears it and returns. If that wake-up were ever lost, the next
vblank would still end the STOP: the cost is up to 1/60 s, not a hang.

**Measured** in Virtual Jaguar with its timing models on. The attract
demos are the fair test: a demo is a fixed list of tics, each drawn once,
so every build draws exactly the same views and only the time differs
(`tools/emu/demobench.py`):

| Detail | Demo 1, before | after | Demo 2, before | after |
|---|---|---|---|---|
| Low | 28.5 frames/s | 29.4 | 28.5 | 29.8 |
| High | 20.7 | 24.9 | 25.1 | 28.0 |
| Ultra | 14.5 | 14.6 | 18.9 | 19.0 |

The GPU's own time per frame is the same before and after to 0.1 ms in
every mode; the gain is all time the GPU used to spend waiting. Over the
24 levels (`tools/emu/detail.py`, as in section 6):

| Detail | Mean, before | after | Slowest level, before | after | Levels at the 30 cap, before | after |
|---|---|---|---|---|---|---|
| Low (160) | 29.1 | 29.8 | 24.8 (MAP21) | 28.4 (MAP02) | 11 | 18 |
| High (320) | 26.1 | 28.2 | 20.2 (MAP17, MAP21) | 22.6 (MAP21) | 7 | 10 |
| Ultra (640) | 19.7 | 19.8 | 13.4 (MAP21) | 13.4 (MAP21) | 1 | 1 |

Both demos play out exactly as before: the player's position at every tic
of both demos matches the previous build.

**A measurement trap.** In Virtual Jaguar with its timing models on, a
68000 that polls GPU RAM makes blits nearly free. Before each 68000 read
of GPU RAM, the emulator brings the GPU up to the 68000's time in a short
slice, and a slice ends by dropping whatever is left of the blitter wait
the GPU was paying. The previous build polled `gpufinished`, in GPU RAM,
for most of every frame. A test build of this version that polls DRAM
instead of stopping is exactly as fast as the STOP build (1563 against
1562 vblanks for 400 Ultra demo frames), and one that polls GPU RAM again
is 4% faster (1497) without being any faster on hardware. So "before"
above is the previous build with only its wait loop changed to poll DRAM,
and the table in section 6 now shows this build. The numbers it replaced
were flattered by the same effect.

**What it costs.** When the GPU is a frame ahead, the picture you see is up
to one frame (33 ms) older than it would have been. The status bar has a
single buffer that is drawn as each frame starts, so it can change up to
two frames before the view it goes with.

**Where 30 isn't reached,** the GPU needs more than 33 ms for every frame of
a stretch, and no amount of buffering helps that. On MAP21 at Low the
views at the start of the level take about 34 ms each; a few turns later
they take 17-28 ms, the GPU is ahead and waits, and the game holds 30. The
68000 isn't the limit: its tic is done about 14 ms after the refresh has
latched, and then it sleeps.

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
   * `test_lzss.py` unpacks the converter's LZSS with the shipped GPU
     decoders (`decomp.gas`, and `R_decode` with its CRY conversion).
   * `test_columns.py` runs the real `R_DrawTexture` and `R_DrawVisSprite`:
     with `FASTCOLUMNS` on and off they must draw **identical pixels** for
     walls and sprites under **both** readings of bug #11 (A2's Y add bit
     honoured, or driven by A1's). Walls must tile their texture at its
     height and never read past it (checked texel by texel against a
     Python reference). Sprites on 320- and 640-column screens, with
     `spropening[]` in main RAM, must stay inside their columns. Spectres
     must be darkened copies of the screen 0-2 rows below and touch nothing
     else. The sky blit, which is inlined in `R_SegLoop`, is checked
     statically for the registers it programs.
   * `R_SetDetail(detail_low)` was checked on the host against the shipped
     160-column tables (all 4,417 entries equal), and in emulated RAM.

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

For the TC version: `tools/build-tc.sh jaguartc.wad freedoom1.wad doom-tc.j64`
(see [section 5](#5-a-complete-game-from-the-jaguar-doom-tc)).

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
5. **Detail.** At High the view and the status bar should line up at the
   left edge as at Low, with twice the detail across. At Ultra, the status
   bar is drawn by scaled objects in the 640-pixel mode; if it is missing or
   doubled, look at the object list `r_phase9.gas` builds (the scaled
   objects' 32-byte alignment and their third phrase). Watch the frame rate
   at High against Low in the same spot, and open the automap at each
   setting.
6. **Triple buffering and STOP.** Movement should look as smooth as before
   or smoother, with no frame shown out of order and no flashes of the old
   Detail mode after changing it. If the frame rate on hardware is far
   below the emulator's, the GPU's interrupt is probably not reaching the 68000 and
   every wait runs to the next vblank: check INT1 bit 1 (TRM p.16) and the
   CPUINT writes in `gpubase.gas` and `r_phase3.gas`.

## Looked at and not done

* **Loading GPU overlays with the blitter** (the manual's recommended method,
  TRM p.36). The resident `gpubase` has no bytes free, the phases rely on
  blitter state set by `R_Setup` that the loader would clobber, and the
  estimated saving is about 1 ms per frame.
* **Deeper light diminishing.** Jaguar lighting subtracts at most
  `(255 - light)/4` from CRY intensity, which is flatter than PC Doom's
  colormaps. Steeper is easy (one shift), but subtractive shading crushes
  dark texels to black instead of scaling them, and it is a matter of taste
  that needs eyes on a screen.

* **Triple buffering at Ultra.** A third 640x180 screen is 230 KB, which
  would come out of the texture cache (section 6).
* **Writing INT2 early in the vblank handler.** The 68000 keeps its raised
  bus priority until it writes INT2 (TRM p.17), which outranks the GPU and
  the blitter. The handler takes about 30 us, the list copy has to finish
  within the vblank anyway, and the manual says to write INT2 at the end.
* **Waking the 68000 from the DSP.** The 68000 still polls DSP RAM while
  the sound mixer finishes before each tic: about 6 ms a frame on MAP21,
  while the GPU walks the BSP. The DSP can interrupt the CPU too (D_CTRL
  CPUINT, TRM p.110), through Jerry's interrupt controller to TOM's INT1
  bit 4. That is the next step; it needs the Jerry side of the interrupt
  plumbing, and the emulator can't show what it saves.

## Licence

The Jaguar Doom source is covered by id Software's limited-use licence in
`license.txt` (non-commercial, educational use). These changes don't alter
that; don't distribute builds for money.

A TC image contains the TC's own data and Freedoom's (BSD licence, see
Freedoom's `COPYING`); it is for your own use, not for distribution.
