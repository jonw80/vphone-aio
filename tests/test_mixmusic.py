#!/usr/bin/env python3
"""
Differential test for mixmusic.gas, using the shipped DSP code as the oracle.

For several consecutive "tics" the same starting state is given to
  A. the shipped sfx.gas     (sound effects only, overwrites the ring)
  B. the shipped music.gas   (music only, overwrites the ring)
  C. mixmusic.gas            (both)
and C must equal saturate16(A + B) sample for sample, leave every other
ring slot alone, and end with the same sfx state as A and the same
sequencer state as B.

A second test checks the one intentional difference: a sound that starts
part-way through a mixing window.  The shipped mixer placed it at 32 bytes
per quad in a 16-byte-per-quad buffer; mixmusic is compared against a
straightforward Python model of the mixing arithmetic instead.

Everything runs on tools/jrisc.py, a functional model of the DSP.
"""
import os
import random
import re
import struct
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
from jrisc import JRISC, s16   # noqa: E402

RMAC = os.environ.get("RMAC", os.path.join(ROOT, "tools", "bin", "rmac"))

# ---- test memory map (DRAM) -------------------------------------------------
SYM = {
    "_samplecount": 0x100000, "_finalquad": 0x100004, "_sfxsample": 0x100008,
    "_soundtics": 0x10000C, "_soundstarttics": 0x100010, "_musictime": 0x100014,
    "_next_eventtime": 0x100018, "_music": 0x10001C, "_music_start": 0x100020,
    "_music_end": 0x100024, "_samples_per_midiclock": 0x100028,
    "_musicvolume": 0x10002C, "_sfxvolume": 0x100030,
    "_sfxchannels": 0x100100, "_music_channels": 0x100200, "_instruments": 0x100400,
}
SFXDATA, INSTDATA, SONG = 0x110000, 0x120000, 0x130000
RING, RINGSAMPLES = 0x1F0000, 4096
DSP_LOAD, DSP_STACK, RET = 0xF1B140, 0xF1CE00, 0xF1B000


ENTRY = {"sfx": "_sfx_start", "music": "_music_dspcode", "mixmusic": "_mixmusic_start"}


def assemble(name):
    """Assemble a DSP job with the externs replaced by test addresses."""
    src = name + ".gas"
    text = open(os.path.join(ROOT, src), "rb").read().decode("latin-1")
    text = re.sub(r"^[ \t]*\.extern[^\n]*\n", "", text, flags=re.M)
    eq = "".join(f"{k}\t.equ\t${v:X}\n" for k, v in SYM.items())
    with tempfile.TemporaryDirectory() as td:
        s, o = os.path.join(td, "t.gas"), os.path.join(td, "t.o")
        open(s, "wb").write((eq + text).encode("latin-1"))
        out = subprocess.run([RMAC, "-fb", s, "-o", o], capture_output=True, text=True)
        assert "rror" not in out.stdout + out.stderr, out.stdout + out.stderr
        d = open(o, "rb").read()
    _m, tsz, _dsz, _b, ssz, _e, trs, drs = struct.unpack(">8I", d[:32])
    assert trs == 0 and drs == 0, f"{src}: unexpected relocations"
    tx = d[32:32 + tsz]
    so, st = 32 + tsz + _dsz, 32 + tsz + _dsz + ssz
    syms = {}
    for i in range(so, st, 12):
        sx, _t, val = struct.unpack(">IIi", d[i:i + 12])
        e = d.index(b"\0", st + sx)
        syms[d[st + sx:e].decode()] = val
    return tx[syms[ENTRY[name]]:syms["codeend"]]


JOBS = {name: assemble(name) for name in ENTRY}


# where each job may write inside DSP RAM: its own image, its sfx buffer, the stack
LOCAL_WRITES = {
    "sfx": [(0xF1B800, 0xF1C800)],
    "music": [],
    "mixmusic": [(0xF1C400, 0xF1CC00)],
}
lowest_stack = {}


def run_job(mem, name):
    cpu = JRISC(dsp=True, mem=mem)
    cpu.load_image(DSP_LOAD, JOBS[name])
    cpu.r[14] = DSP_STACK
    cpu.r[28] = RET
    allowed = LOCAL_WRITES[name] + [(DSP_LOAD, DSP_LOAD + len(JOBS[name]))]

    def watch(addr, size, value):
        if 0xF1B000 <= addr < 0xF1E000:
            if any(lo <= addr < hi for lo, hi in allowed):
                return
            assert 0xF1CC00 <= addr < DSP_STACK, f"{name}: stray DSP RAM write at ${addr:06X}"
            lowest_stack[name] = min(lowest_stack.get(name, DSP_STACK), addr)
    cpu.watch = watch
    cpu.run(DSP_LOAD, RET)
    return cpu.steps


# ---- scene ------------------------------------------------------------------
def w32(mem, a, v):
    mem[a:a + 4] = (v & 0xFFFFFFFF).to_bytes(4, "big")


def r32(mem, a):
    return int.from_bytes(mem[a:a + 4], "big")


def ring(mem):
    return [s16(int.from_bytes(mem[RING + 2 * i:RING + 2 * i + 2], "big")) for i in range(RINGSAMPLES)]


def build_scene(rng, samplecount, mid_window_sound=False):
    mem = bytearray(1 << 24)
    finalquad = samplecount // 8 + 512 - 184          # last tic mixed ~1470 samples ago
    w32(mem, SYM["_samplecount"], samplecount)
    w32(mem, SYM["_finalquad"], finalquad)
    w32(mem, SYM["_sfxvolume"], 200)
    w32(mem, SYM["_musicvolume"], 200)               # equal, so music.gas matches
    # ring starts out with noise so stray writes are visible
    for i in range(RINGSAMPLES):
        mem[RING + 2 * i:RING + 2 * i + 2] = rng.randrange(-3000, 3000).to_bytes(2, "big", signed=True)
    # sound effects: 8-bit unsigned samples packed four to a long
    for i in range(0x8000):
        mem[SFXDATA + i] = rng.randrange(256)
    chans = [
        (SFXDATA, finalquad, finalquad + 900, 127 * 200),         # starts with the window
        (SFXDATA + 0x2000, finalquad - 40, finalquad + 120, 90 * 200),   # already playing, ends mid-window
        (SFXDATA + 0x4000, finalquad - 900, finalquad - 10, 127 * 200),  # finished
        (SFXDATA + 0x6000, finalquad + 30 if mid_window_sound else finalquad - 5,
         finalquad + 400, 60 * 200),
    ]
    for i, (src, sq, eq, vol) in enumerate(chans):
        base = SYM["_sfxchannels"] + 24 * i
        w32(mem, base, src)
        w32(mem, base + 4, sq)
        w32(mem, base + 8, eq)
        w32(mem, base + 12, vol)
    # instruments (sfx_t: samples, loop_start, loop_end, info, unity, pitch_corr, decay, data[])
    for n in range(3):
        a = INSTDATA + 0x4000 * n
        length = 6000 + 2000 * n
        for k, v in enumerate((length, 100, length, 2 if n == 1 else 0, 60, 0, 0)):
            w32(mem, a + 4 * k, v)
        for k in range(length + 64):
            mem[a + 28 + k] = rng.randrange(256)
        w32(mem, SYM["_instruments"] + 4 * n, a)
    # song: [delta][event]..., see M_GetEvent
    song = bytes([
        0, 0x30, 0, 0, 0, 90,                      # tempo: 90 samples per midi clock
        0, 0x10, 0, 100, 0, 0x01, 0x00, 0x00,      # ch0 note on, inst 0, vel 100, step 1.0
        0, 0x11, 1, 70, 0, 0x00, 0xC0, 0x00,       # ch1 note on, inst 1 (looping), step 0.75
        3, 0x12, 2, 127, 0, 0x01, 0x80, 0x00,      # ch2 note on, inst 2, step 1.5
        5, 0x41, 40, 0,                            # ch1 volume 40
        4, 0x20,                                   # ch0 off
        6, 0x52, 0x00, 0xE0, 0x00,                 # ch2 pitch 0.875
        2, 0x00,                                   # no-op
        9, 0x10, 1, 90, 0, 0x01, 0x20, 0x00,       # ch0 note on again
        12, 0x21,                                  # ch1 off
        40, 0x22,                                  # ch2 off
        30, 0x00,
    ])
    mem[SONG:SONG + len(song)] = song
    w32(mem, SYM["_music"], SONG)
    w32(mem, SYM["_music_start"], SONG)          # looping
    w32(mem, SYM["_music_end"], SONG + len(song))
    return mem


def sfx_state(mem):
    return (r32(mem, SYM["_finalquad"]), r32(mem, SYM["_sfxsample"]),
            bytes(mem[SYM["_sfxchannels"]:SYM["_sfxchannels"] + 96]))


def music_state(mem):
    keys = ("_musictime", "_next_eventtime", "_music", "_samples_per_midiclock")
    return tuple(r32(mem, SYM[k]) for k in keys) + (
        bytes(mem[SYM["_music_channels"]:SYM["_music_channels"] + 280]),)


def sat16(v):
    return max(-0x8000, min(0x7FFF, v))


# ---- tests ------------------------------------------------------------------
def test_matches_shipped_code(tics=6):
    rng = random.Random(1)
    samplecount = 0x40000
    base = build_scene(rng, samplecount)
    a, b, c = bytearray(base), bytearray(base), bytearray(base)
    start = r32(base, SYM["_finalquad"]) * 8 + 4096
    for m in (b,):                                  # the shipped path needed this set up by C
        w32(m, SYM["_musictime"], start)
        w32(m, SYM["_next_eventtime"], start)
    initial = ring(base)
    written = set()
    steps = {"sfx": 0, "music": 0, "mixmusic": 0}
    for tic in range(tics):
        f_old = r32(a, SYM["_finalquad"])
        steps["sfx"] += run_job(a, "sfx")
        steps["music"] += run_job(b, "music")
        steps["mixmusic"] += run_job(c, "mixmusic")
        f_new = r32(a, SYM["_finalquad"])
        written |= {q % RINGSAMPLES for q in range(f_old * 8, f_new * 8)}
        ra, rb, rc = ring(a), ring(b), ring(c)
        for i in range(RINGSAMPLES):
            if i in written:
                assert rc[i] == sat16(ra[i] + rb[i]), f"tic {tic} slot {i}: {rc[i]} != sat({ra[i]}+{rb[i]})"
            else:
                assert rc[i] == initial[i], f"tic {tic}: stray write at slot {i}"
        assert sfx_state(c) == sfx_state(a), f"tic {tic}: sfx state differs"
        assert music_state(c) == music_state(b), f"tic {tic}: music state differs"
        assert any(rb[i] != initial[i] for i in written), "music produced nothing"
        # advance time by one 15 Hz tic, keeping samplecount a multiple of 8
        samplecount += 1472
        for m in (a, b, c):
            w32(m, SYM["_samplecount"], samplecount)
    per_tic = {k: v // tics for k, v in steps.items()}
    print(f"  ok  mixmusic == sat(sfx + music) over {tics} tics, "
          f"{len(written)} ring slots; DSP instructions per tic: {per_tic}")


def test_sound_starting_mid_window():
    rng = random.Random(2)
    mem = build_scene(rng, 0x40000, mid_window_sound=True)
    w32(mem, SYM["_music"], 0)                      # sfx only
    startquad = r32(mem, SYM["_finalquad"])
    initial = ring(mem)
    run_job(mem, "mixmusic")
    stopquad = r32(mem, SYM["_finalquad"])
    # reference model of x_MixSfx / x_PaintChannel / x_S_WriteOutSamples
    acc = [0] * ((stopquad - startquad) * 4)
    for i in range(4):
        base = SYM["_sfxchannels"] + 24 * i
        src, sq, eq, vol = (r32(build_scene(random.Random(2), 0x40000, True), base + 4 * k) for k in range(4))
        lo, hi = max(sq, startquad), min(eq, stopquad)
        for q in range(lo, hi):
            for k in range(4):
                byte = mem[src + (q - sq) * 4 + k]
                acc[(q - startquad) * 4 + k] += (byte - 128) * s16(vol)
    out = ring(mem)
    for j, v in enumerate(acc):
        smp = sat16(v >> 8)
        for dup in (0, 1):                          # 11 kHz sfx doubled for the 22 kHz isr
            slot = (startquad * 8 + 2 * j + dup) % RINGSAMPLES
            assert out[slot] == smp, f"sample {j}: {out[slot]} != {smp}"
    touched = {(startquad * 8 + i) % RINGSAMPLES for i in range((stopquad - startquad) * 8)}
    assert all(out[i] == initial[i] for i in range(RINGSAMPLES) if i not in touched)
    print(f"  ok  sound starting mid-window mixes at the right offset "
          f"({(stopquad - startquad) * 8} samples checked)")


def test_clock_catches_up():
    """A music clock left behind the sfx window jumps forward, and pending
    events move with it: same result as starting on time."""
    rng = random.Random(3)
    base = build_scene(rng, 0x48000)
    start = r32(base, SYM["_finalquad"]) * 8 + 4096
    late, ontime = bytearray(base), bytearray(base)
    w32(late, SYM["_musictime"], start - 5000)
    w32(late, SYM["_next_eventtime"], start - 5000)
    w32(ontime, SYM["_musictime"], start)
    w32(ontime, SYM["_next_eventtime"], start)
    run_job(late, "mixmusic")
    run_job(ontime, "mixmusic")
    assert ring(late) == ring(ontime)
    assert music_state(late) == music_state(ontime)
    print("  ok  a lagging music clock resyncs to the sfx window")


if __name__ == "__main__":
    test_matches_shipped_code()
    test_sound_starting_mid_window()
    test_clock_catches_up()
    used = {k: DSP_STACK - v for k, v in lowest_stack.items()}
    assert used.get("mixmusic", 0) <= 0x200, used
    print(f"  ok  DSP RAM writes stay in bounds; stack bytes used: {used}")
