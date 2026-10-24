# Patches for Virtual Jaguar 3.10.0

Applied to libretro/virtualjaguar-libretro at `e7db536` (tag v3.10.0) by
`.github/workflows/virtualjaguar-macos.yml`, which builds the macOS app with them.

* `0001-gpu-carry-stall-overrun.patch`: `GPUExec()` dropped whatever a bus or blitter
  stall overran its slice by, so with the experimental timing options on blits came out
  too cheap. A 68000 that polls GPU RAM, as Jaguar Doom's does, cuts the GPU into tiny
  slices and made blits nearly free. The overrun is now carried into the next slice.
  With the timing options off nothing changes. Measured on 400 frames of a Doom attract
  demo at 640 columns: every way of waiting now costs the same GPU time (79.7 ms a frame;
  before, 53-57 ms depending on how the 68000 waited). `make test` passes. The new
  counter is not saved in savestates, so a loaded state can differ by a few cycles.
* `0002-default-bindings-keypad.patch`: the default keyboard no longer binds A S D / Q E
  to keypad 9 8 7 / 4 6, and the default gamepad no longer puts keypad 7 8 9 * on the
  right stick. Pressing those letters, or a drifting stick, pressed keypad keys in
  whatever game was running; in Jaguar Doom keypad 9 opens the automap, and with Pause
  it warps to area 9. The keys can be bound again under Settings > Input.
