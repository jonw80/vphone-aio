#!/usr/bin/env python3
"""
endings.py - check the episode endings in the emulator.

    tools/emu/endings.py jagdoom.wad doom.j64 outdir e1m8|finale

e1m8:   E1M8's baron fight: flip the start switch, ride the lift into the
        star, cross the cage line, kill both barons (god mode, plasma
        rifle), and watch the tag-666 floor that A_BossDeath lowers.
finale: E3M8 (Dis): the switch, the yellow door (with all keycards, as the
        game's Pause+# cheat gives) and the exit line, then the
        intermission, the end text and the cast call.

Screenshots go to outdir.  A test driver like autoplay.py: god mode and
the weapons are set in RAM; everything else is played.
"""
import math
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from drive import Emu                         # noqa: E402
from autoplay import Level, Pilot, PL_CHEATS   # noqa: E402

WAD, ROM, OUT = (os.path.abspath(p) for p in sys.argv[1:4]) if len(sys.argv) > 4 else (None,) * 3
PL_WEAPONS, PL_AMMO, PL_PENDING = 108, 140, 104
SECTOR_SIZE, MT_BRUISER = 96, 7


def start_map(e, pilot, m, skill=2):
    e.run(150)
    e.run(4, "B")
    e.run(40)
    e.poke(e.syms["_maxlevel"], (24).to_bytes(4, "big"))
    e.poke(e.syms["_playermap"], m.to_bytes(4, "big"))
    e.poke(e.syms["_playerskill"], skill.to_bytes(4, "big"))
    e.run(20)
    e.run(4, "B")
    while True:
        st = pilot.state()
        if st and (st[0], st[1]) != (0, 0):
            break
        e.run(10)
    e.run(30)


def arm(e):
    p = e.syms["_players"]
    c = p + PL_CHEATS
    e.poke(c, (e.u32(c) | 2).to_bytes(4, "big"))                     # god mode
    e.poke(p + PL_WEAPONS, struct.pack(">8i", *[1] * 8))             # every weapon
    e.poke(p + PL_AMMO, struct.pack(">4i", 400, 100, 400, 100))      # clips shells cells rockets
    e.poke(p + PL_PENDING, (5).to_bytes(4, "big"))                   # switch to the plasma rifle
    e.run(60)


def line_goal(L, li, depth=(24, 32, 40), side=1):
    ln = L.lines[li]
    (x1, y1), (x2, y2) = L.v[ln[0]], L.v[ln[1]]
    mx, my = (x1 + x2) / 2, (y1 + y2) / 2
    nx, ny = (y2 - y1), -(x2 - x1)
    k = math.hypot(nx, ny) or 1
    return [(mx + side * nx / k * d, my + side * ny / k * d) for d in depth], (mx, my)


def lowest_neighbour_floor(L, s):
    fl = []
    for li in range(len(L.lines)):
        f, b = L.line_sectors(li)
        if s in (f, b) and b is not None:
            o = b if f == s else f
            fl.append(L.sectors[o][0])
    return min(fl) if fl else L.sectors[s][0]


def set_sector(L, s, floor=None, ceil=None):
    f, c, *rest = L.sectors[s]
    L.sectors[s] = (floor if floor is not None else f, ceil if ceil is not None else c, *rest)
    L._block = {}


def barons(e):
    out = []
    head = e.syms["_mobjhead"]
    m = e.u32(head + 4)
    while m and m != head:
        d = e.peek(m, 108)
        typ, hp = struct.unpack(">i", d[84:88])[0], struct.unpack(">i", d[104:108])[0]
        if typ == MT_BRUISER:
            out.append(hp)
        m = struct.unpack(">I", d[4:8])[0]
    return out


def sector_floor(e, s):
    base = e.u32(e.syms["_sectors"])
    return e.s32(base + s * SECTOR_SIZE) / 65536


def e1m8():
    L = Level(WAD, 8)
    e = Emu(ROM)
    pilot = Pilot(e, OUT)
    start_map(e, pilot, 8)
    arm(e)
    pilot.shot("e1m8-start")
    print("barons alive:", barons(e), " tag-666 floor:", sector_floor(e, 62))
    x, y, _a, _ = pilot.state()
    # 1. the start room's switch (S1 lower floor, tag 1) opens the way out
    sw = next(i for i, ln in enumerate(L.lines) if ln[3] == 23)
    pts, face = line_goal(L, sw)
    r = L.route((x, y), pts)
    print("switch route", r and len(r))
    pilot.goals = pts
    pilot.drive(L, r, 30 * 60, use_at_end=True, face=face, every=99999, tag="e1m8")
    e.run(180)
    for s in (i for i, sec in enumerate(L.sectors) if sec[4] == 1):
        set_sector(L, s, floor=lowest_neighbour_floor(L, s))
    pilot.shot("e1m8-switch")
    # 2. the lift up into the star (SR lower lift, tag 6): Use it from the
    #    corridor, step on, ride it up
    lift = next(i for i, ln in enumerate(L.lines) if ln[3] == 62)
    x, y, _a, _ = pilot.state()
    pts, face = line_goal(L, lift)
    r = L.route((x, y), pts)
    print("lift route", r and len(r))
    pilot.goals = pts
    pilot.drive(L, r, 40 * 60, use_at_end=True, face=face, every=99999, tag="e1m8")
    e.run(40)
    e.run(50, "U")
    e.run(240)
    pilot.shot("e1m8-lift")
    print("after the lift:", tuple(round(v) for v in pilot.state()[:2]))
    # 3. walk over the cage line in the star (W1 open door, tag 5)
    cage = next(i for i, ln in enumerate(L.lines) if ln[3] == 2)
    x, y, _a, _ = pilot.state()
    best = None
    for side in (1, -1):
        pts, _ = line_goal(L, cage, depth=(40, 56), side=side)
        r = L.route((x, y), pts)
        if r and (best is None or len(r) < len(best)):
            best = r
    print("route into the star", best and len(best))
    pilot.goals = None
    pilot.drive(L, best, 60 * 60, every=240, tag="e1m8", shoot=True)
    for s in (i for i, sec in enumerate(L.sectors) if sec[4] == 5):
        set_sector(L, s, ceil=200)
    # 4. fight until both barons are dead, watching the tag-666 floor
    t0 = e.frame
    seen_drop = None
    while e.frame - t0 < 120 * 60:
        pilot.skirmish(L, 60, 30, "e1m8-fight")
        b = barons(e)
        f = sector_floor(e, 62)
        print(f"  t+{(e.frame - t0) // 60:3d}s barons {b} tag-666 floor {f:.0f}")
        if b and all(h <= 0 for h in b):
            if seen_drop is None:
                pilot.shot("e1m8-barons-dead")
            seen_drop = f
            if f < 176:
                break
    for _ in range(4):
        e.run(60)
        print(f"  after: tag-666 floor {sector_floor(e, 62):.0f}")
    pilot.shot("e1m8-after")
    e.close()


def finale():
    L = Level(WAD, 23)
    e = Emu(ROM)
    pilot = Pilot(e, OUT)
    start_map(e, pilot, 23)
    arm(e)
    # every keycard (what the game's own Pause+# cheat gives); the exit is
    # behind a yellow door
    e.poke(e.syms["_players"] + 64, struct.pack(">6i", *[1] * 6))
    import autoplay
    autoplay.LOCKED.clear()
    pilot.shot("e3m8")

    def reach(li, use, far=False):
        x, y, _a, _ = pilot.state()
        best = None
        for side in (1, -1):
            pts, face = line_goal(L, li, depth=(24, 32, 40) if use else (40, 56), side=side)
            r = L.route((x, y), pts)
            d = math.hypot(pts[0][0] - x, pts[0][1] - y)
            if r and (best is None or (far and d > best[3]) or (not far and len(r) < len(best[0]))):
                best = (r, pts, face, d)
        return best

    # the switch that opens the exit door (S1 open door, tag 26)
    sw = next(i for i, ln in enumerate(L.lines) if ln[3] == 103)
    r, pts, face, _ = reach(sw, True)
    print("switch route", len(r))
    pilot.goals = pts
    pilot.drive(L, r, 90 * 60, use_at_end=True, face=face, every=600, tag="e3m8")
    e.run(120)
    for s_ in (i for i, sec in enumerate(L.sectors) if sec[4] == 26):
        set_sector(L, s_, ceil=128)
    pilot.shot("e3m8-door")
    # open the yellow door in front of the exit
    door = next(i for i, ln in enumerate(L.lines) if ln[3] == 34)
    r, pts, face, _ = reach(door, True)
    print("yellow door route", len(r))
    pilot.goals = pts
    pilot.drive(L, r, 60 * 60, use_at_end=True, face=face, every=600, tag="e3m8")
    e.run(90)
    pilot.shot("e3m8-yellow-door")
    # walk over the exit line
    ex = next(i for i, ln in enumerate(L.lines) if ln[3] == 52)
    r, pts, face, _ = reach(ex, False, far=True)
    print("exit route", r and len(r))
    pilot.goals = None
    before = e.sym32("_ticon")
    pilot.drive(L, r, 60 * 60, every=600, tag="e3m8", shoot=False)
    print("level over:", e.sym32("_ticon") < before or pilot.state() is None)
    for i in range(12):
        e.run(30)
        if i in (3, 8):
            pilot.shot("e3m8-intermission")
    e.run(4, "B")                        # leave the intermission: the finale follows
    for i in range(80):
        e.run(60)
        if i % 3 == 0:
            pilot.shot("finale")
        if i > 40 and i % 6 == 0:
            e.run(4, "B")                # move the cast call along
    e.close()


if __name__ == "__main__":
    if len(sys.argv) != 5 or sys.argv[4] not in ("e1m8", "finale"):
        sys.exit(__doc__)
    os.makedirs(OUT, exist_ok=True)
    {"e1m8": e1m8, "finale": finale}[sys.argv[4]]()
