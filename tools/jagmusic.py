#!/usr/bin/env python3
"""
jagmusic.py - MIDI songs and GENMIDI patches -> Jaguar Doom music lumps.

The Jaguar music sequencer (music.gas, and mixmusic.gas in this build) has
ten voices that each play a sampled instrument (an sfx_t: 8-bit unsigned
samples read at a 8.16 fixed-point step per 22050 Hz output sample).  Its
song format, as M_PaintMusic/M_GetEvent read it, is a stream of

    [delta][command...]          delta: one byte, in "MIDI clocks"

with the command's high nibble selecting

    0x00  nothing (used to wait longer than 255 clocks)
    0x1v  note on, voice v: instrument, velocity, pad, step (3 bytes)
    0x2v  note off, voice v (stops it at once)
    0x30  tempo: samples per clock (4 bytes)
    0x4v  volume, voice v: velocity, pad
    0x5v  pitch, voice v: step (3 bytes)

and the instruments are the lumps between INSTSTRT and INSTEND, named
Innn (melodic, instrument number nnn) or Pnnn (percussion, nnn + 128).

Instruments are not stored in a MIDI file, so they are synthesized here
from the OPL2 FM patches in the IWAD's GENMIDI lump - the patch set the PC
version played these songs with - with a small two-operator FM model.
There is no volume envelope in the Jaguar sequencer: the patch's attack,
decay and sustain are rendered into the sample, a sustaining patch gets a
loop over its steady part, and note off cuts the voice.
"""
import math
import struct

RATE = 22050
CLOCK = 147                      # samples per sequencer clock: 150 clocks/s
LOOP = 168                       # two periods at the base pitch (MULT 0.5 safe)
F0 = RATE / (LOOP / 2)           # melodic samples are rendered at 262.5 Hz
VOICES = 10
MAX_MELODIC = 6000               # samples (0.27 s at the base pitch)
MAX_SUSTAIN_ATTACK = 3000
MAX_PERC = 6000
PEAK = 90                        # leaves headroom for ten voices + sfx

MULT = [0.5, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 10, 12, 12, 15, 15]
ATTACK_MS = [None, 2826.24, 1413.12, 706.56, 353.28, 176.64, 88.32, 44.16,
             22.08, 11.04, 5.52, 2.76, 1.38, 0.69, 0.35, 0.0]
DECAY_MS = [None, 39280.64, 19640.32, 9820.16, 4910.08, 2455.04, 1227.52, 613.76,
            306.88, 153.44, 76.72, 38.36, 19.18, 9.59, 4.79, 2.40]


# ---------------------------------------------------------------------------
# MIDI

def _vlq(d, p):
    v = 0
    while True:
        b = d[p]
        p += 1
        v = (v << 7) | (b & 0x7F)
        if not b & 0x80:
            return v, p


def parse_midi(d):
    """-> sorted [(seconds, kind, channel, a, b)]"""
    if d[:4] != b"MThd":
        raise ValueError("not a MIDI file")
    hl = struct.unpack(">I", d[4:8])[0]
    _fmt, ntrk, div = struct.unpack(">HHH", d[8:14])
    if div & 0x8000:
        raise ValueError("SMPTE time division is not supported")
    p = 8 + hl
    raw, tempos = [], [(0, 500000)]
    for t in range(ntrk):
        if d[p:p + 4] != b"MTrk":
            break
        ln = struct.unpack(">I", d[p + 4:p + 8])[0]
        q, end = p + 8, p + 8 + ln
        tick, running, seq = 0, None, 0
        while q < end:
            delta, q = _vlq(d, q)
            tick += delta
            st = d[q]
            if st & 0x80:
                q += 1
                if st < 0xF0:
                    running = st
            else:
                st = running
            if st == 0xFF:
                typ = d[q]
                ln2, q = _vlq(d, q + 1)
                if typ == 0x51:
                    tempos.append((tick, int.from_bytes(d[q:q + 3], "big")))
                q += ln2
                if typ == 0x2F:
                    break
            elif st in (0xF0, 0xF7):
                ln2, q = _vlq(d, q)
                q += ln2
            else:
                hi, ch = st & 0xF0, st & 15
                if hi in (0xC0, 0xD0):
                    a, b = d[q], 0
                    q += 1
                else:
                    a, b = d[q], d[q + 1]
                    q += 2
                raw.append((tick, t, seq, hi, ch, a, b))
                seq += 1
        p = end
    tempos.sort()
    # tick -> seconds through the tempo map
    marks, sec, last_tick, us = [], 0.0, 0, 500000
    for tk, tempo in tempos:
        sec += (tk - last_tick) * us / div / 1e6
        marks.append((tk, sec, tempo))
        last_tick, us = tk, tempo

    def seconds(tick):
        lo = 0
        for i, (tk, _s, _t) in enumerate(marks):
            if tk <= tick:
                lo = i
        tk, s, tempo = marks[lo]
        return s + (tick - tk) * tempo / div / 1e6
    raw.sort(key=lambda e: (e[0], e[1], e[2]))
    return [(seconds(tk), hi, ch, a, b) for tk, _t, _s, hi, ch, a, b in raw]


# ---------------------------------------------------------------------------
# GENMIDI patches and the FM model

def parse_genmidi(d):
    if not d or d[:8] != b"#OPL_II#":
        raise ValueError("no GENMIDI lump")
    out = []
    for i in range(175):
        r = d[8 + 36 * i:8 + 36 * (i + 1)]
        flags, finetune, note = struct.unpack("<HBB", r[:4])
        voices = []
        for v in range(2):
            b = r[4 + 16 * v:4 + 16 * (v + 1)]
            mod = dict(char=b[0], ad=b[1], sr=b[2], wave=b[3], ksl=b[4], level=b[5])
            car = dict(char=b[7], ad=b[8], sr=b[9], wave=b[10], ksl=b[11], level=b[12])
            offset = struct.unpack("<h", b[14:16])[0]
            voices.append(dict(mod=mod, car=car, fb=b[6], offset=offset))
        out.append(dict(flags=flags, finetune=finetune, note=note, voices=voices))
    return out


def _wave(kind, ph):
    s = math.sin(ph)
    k = kind & 3
    if k == 0:
        return s
    if k == 1:
        return s if s > 0 else 0.0
    if k == 2:
        return abs(s)
    return abs(s) if (ph / (math.pi / 2)) % 2 < 1 else 0.0


class _Env:
    """OPL envelope (key held): attack, decay to sustain, then hold or fall."""

    def __init__(self, op):
        self.ar, self.dr = op["ad"] >> 4, op["ad"] & 15
        self.sl, self.rr = op["sr"] >> 4, op["sr"] & 15
        self.hold = bool(op["char"] & 0x20)
        self.tl = (op["level"] & 0x3F) * 0.75
        self.sl_db = 93.0 if self.sl == 15 else self.sl * 3.0

    def db(self, t):
        """attenuation in dB at t seconds after key on"""
        a = ATTACK_MS[self.ar]
        if a is None:
            return 96.0
        a /= 1000
        if t < a:
            x = t / a
            return 96.0 * (1 - x) ** 3                    # fast exponential-ish rise
        t -= a
        d = DECAY_MS[self.dr]
        if d is None:
            return 0.0
        rate = 96.0 / (d / 1000)                          # dB per second
        if t * rate < self.sl_db:
            return t * rate
        t -= self.sl_db / rate
        if self.hold:
            return self.sl_db
        r = DECAY_MS[self.rr]
        if r is None:
            return self.sl_db
        return min(96.0, self.sl_db + t * 96.0 / (r / 1000))

    def amp(self, t):
        return 10 ** (-(self.db(t) + self.tl) / 20)


def _render_voice(v, freq, n):
    mod, car = v["mod"], v["car"]
    em, ec = _Env(mod), _Env(car)
    mm, mc = MULT[mod["char"] & 15], MULT[car["char"] & 15]
    fb = (v["fb"] >> 1) & 7
    additive = v["fb"] & 1
    fbk = 0.0 if fb == 0 else 4 * math.pi * 2 ** (fb - 7)
    w = 2 * math.pi * freq / RATE
    out = [0.0] * n
    o1 = o2 = 0.0
    for i in range(n):
        t = i / RATE
        mo = _wave(mod["wave"], w * mm * i + fbk * (o1 + o2) / 2) * em.amp(t)
        o2, o1 = o1, mo
        if additive:
            co = _wave(car["wave"], w * mc * i) * ec.amp(t)
            out[i] = co + mo
        else:
            out[i] = _wave(car["wave"], w * mc * i + 4 * math.pi * mo) * ec.amp(t)
    return out


def _length(patch, cap):
    """samples until the carrier is ~silent (or reaches a held sustain)"""
    v = patch["voices"][0]
    ec = _Env(v["car"])
    if ec.hold or v["fb"] & 1 and _Env(v["mod"]).hold:
        t = 0.0
        while t < cap / RATE and ec.db(t) < ec.sl_db - 0.5:
            t += 0.002
        return min(cap, int(t * RATE)), True
    t = 0.0
    while t < cap / RATE and ec.db(t) < 48:
        t += 0.002
    return max(512, min(cap, int(t * RATE))), False


def synth(patch, freq, cap, allow_loop=True):
    """-> (samples (bytes), loop_start, loop_end) or loop None"""
    n, sustains = _length(patch, cap)
    loop = None
    if sustains and allow_loop:
        n = min(n, MAX_SUSTAIN_ATTACK) + 2 * LOOP
        loop = (n - LOOP, n)
    elif sustains:
        n = cap                                            # no loop: play it out
    sig = _render_voice(patch["voices"][0], freq, n)
    if patch["flags"] & 4:                                 # double voice
        v2 = patch["voices"][1]
        f2 = freq * 2 ** ((v2["offset"] - patch["voices"][0]["offset"]) / 12)
        f2 *= 2 ** ((patch["finetune"] - 128) / 64 / 12)
        sig2 = _render_voice(v2, f2, n)
        sig = [a + b for a, b in zip(sig, sig2)]
    peak = max(1e-9, max(abs(x) for x in sig))
    g = PEAK / peak
    if loop is None:
        fade = min(256, n // 4)
        for i in range(fade):
            sig[n - 1 - i] *= i / fade
    data = bytes(max(0, min(255, int(round(128 + g * x)))) for x in sig)
    return data, loop


def inst_lump(data, loop, percussion=False):
    info = (2 if loop else 0) | (1 if percussion else 0)
    ls, le = loop if loop else (0, 0)
    pad = (data[ls:ls + 16] if loop else b"\x80" * 16)
    return struct.pack(">7i", len(data), ls, le, info, 60, 0, 0) + data + pad


def note_freq(n):
    return 440.0 * 2 ** ((n - 69) / 12)


# ---------------------------------------------------------------------------
# song conversion

def _step(ratio):
    v = int(round(ratio * 65536))
    return max(1, min(0xFFFFFF, v))


def convert_song(events, patches, loop=True, max_seconds=150.0):
    """-> (song bytes, melodic programs used, percussion notes used)"""
    progs, percs = set(), set()
    out = []                                              # (clock, bytes)
    prog = [0] * 16
    vol = [100] * 16
    expr = [127] * 16
    bend = [0.0] * 16
    pedal = [False] * 16
    voices = [None] * VOICES                              # dicts
    perc_len = {}

    def clock(t):
        return int(round(t * RATE / CLOCK))

    def volume(ch, vel):
        return max(1, min(127, vel * vol[ch] * expr[ch] // (127 * 127)))

    def step_for(ch, note):
        p = patches[prog[ch]]
        off = p["voices"][0]["offset"]
        return _step(note_freq(note + off + bend[ch]) / F0)

    def alloc(t):
        for i, v in enumerate(voices):
            if v is None or (v["until"] is not None and v["until"] <= t):
                return i
        return min(range(VOICES), key=lambda i: voices[i]["start"])

    end_t = 0.0
    for t, kind, ch, a, b in events:
        if t > max_seconds:
            break
        end_t = t
        c = clock(t)
        if kind == 0x90 and b > 0:
            if ch == 9:
                if not 35 <= a <= 81:
                    continue
                percs.add(a)
                i = alloc(t)
                voices[i] = dict(ch=ch, note=a, vel=b, start=t,
                                 until=t + perc_len.get(a, MAX_PERC / RATE), off=False)
                out.append((c, bytes([0x10 | i, 128 + a, volume(ch, b), 0, 1, 0, 0])))
            else:
                progs.add(prog[ch])
                i = alloc(t)
                voices[i] = dict(ch=ch, note=a, vel=b, start=t, until=None, off=False)
                s = step_for(ch, a)
                out.append((c, bytes([0x10 | i, prog[ch], volume(ch, b), 0,
                                      s >> 16, (s >> 8) & 255, s & 255])))
        elif kind == 0x80 or (kind == 0x90 and b == 0):
            if ch == 9:
                continue
            for i, v in enumerate(voices):
                if v and v["ch"] == ch and v["note"] == a and v["until"] is None:
                    if pedal[ch]:
                        v["off"] = True
                    else:
                        out.append((c, bytes([0x20 | i])))
                        voices[i] = None
                    break
        elif kind == 0xB0:
            if a in (7, 11):
                if a == 7:
                    vol[ch] = b
                else:
                    expr[ch] = b
                for i, v in enumerate(voices):
                    if v and v["ch"] == ch:
                        out.append((c, bytes([0x40 | i, volume(ch, v["vel"]), 0])))
            elif a == 64:
                pedal[ch] = b >= 64
                if not pedal[ch]:
                    for i, v in enumerate(voices):
                        if v and v["ch"] == ch and v["off"]:
                            out.append((c, bytes([0x20 | i])))
                            voices[i] = None
            elif a in (120, 123):
                for i, v in enumerate(voices):
                    if v and v["ch"] == ch:
                        out.append((c, bytes([0x20 | i])))
                        voices[i] = None
        elif kind == 0xC0:
            prog[ch] = a & 127
        elif kind == 0xE0 and ch != 9:
            bend[ch] = ((b << 7 | a) - 8192) / 8192 * 2.0
            for i, v in enumerate(voices):
                if v and v["ch"] == ch and v["until"] is None:
                    s = step_for(ch, v["note"])
                    out.append((c, bytes([0x50 | i, s >> 16, (s >> 8) & 255, s & 255])))
    # all notes off at the end, then a short rest before the loop
    c = clock(end_t)
    for i, v in enumerate(voices):
        if v:
            out.append((c + 1, bytes([0x20 | i])))
    out.append((c + (75 if loop else 1), b"\0"))
    song = bytearray([0, 0x30]) + CLOCK.to_bytes(4, "big")
    last = 0
    for c, cmd in out:
        d = c - last
        while d > 255:
            song += bytes([255, 0])
            d -= 255
        song.append(d)
        song += cmd
        last = c
    return bytes(song), progs, percs


def convert(songs, genmidi, log=print, loop=None):
    """songs: {lump name: MIDI bytes or None} -> ([(inst name, lump)], {name: song})"""
    patches = parse_genmidi(genmidi)
    loop = loop or {}
    events, progs, percs = {}, set(), set()
    parsed = {}
    for name, d in songs.items():
        parsed[name] = parse_midi(d) if d else []
    for name, ev in parsed.items():
        song, p, q = convert_song(ev, patches, loop.get(name, True))
        events[name] = song
        progs |= p
        percs |= q
    insts = []
    for pnum in sorted(progs):
        data, lp = synth(patches[pnum], F0, MAX_MELODIC)
        insts.append((f"I{pnum:03d}", inst_lump(data, lp)))
    for note in sorted(percs):
        patch = patches[128 + note - 35]
        fixed = patch["note"] if patch["flags"] & 1 else 60
        f = note_freq(fixed + patch["voices"][0]["offset"])
        data, lp = synth(patch, f, MAX_PERC, allow_loop=False)
        insts.append((f"P{note:03d}", inst_lump(data, None, percussion=True)))
    total = sum(len(d) for _, d in insts) + sum(len(s) for s in events.values())
    log(f"  music: {len(events)} songs, {len(insts)} instruments ({len(progs)} melodic, "
        f"{len(percs)} percussion), {total // 1024} KB")
    return insts, events
