#!/usr/bin/env python3
"""
autoplay.py - play Jaguar Doom in the emulator with a simple autopilot.

    tools/emu/autoplay.py jagdoom.wad doom.j64 outdir [--map N] [--seconds S]

The autopilot reads the level from the WAD (the same lumps the game
loads), plans a route to the exit on a 16-unit grid, then steers by
reading the player's position from emulated RAM every two frames:
turn toward the next waypoint, walk, open doors with Use when stuck,
and turn to shoot monsters that come close.  It takes screenshots along
the way and records the audio to a WAV file.

It is a test driver, not a bot worth playing against: no keys, no
lifts, no dodging.
"""
import argparse
import heapq
import math
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from drive import Emu                      # noqa: E402
from mkwad import lzss_decode              # noqa: E402

MO_X, MO_ANGLE, MO_MOMX, MO_TYPE, MO_FLAGS, MO_HEALTH = 12, 32, 72, 84, 100, 104
MF_SHOOTABLE, MF_COUNTKILL = 4, 0x400000
DEMOBUF, DEMOEND = 0x1DE100, 0x1E0000
PL_CHEATS = 180                    # offsetof(player_t, cheats)
DOORS = {1, 31, 117, 118, 26, 27, 28, 32, 33, 34}     # opened with Use
LOCKED = {26, 27, 28, 32, 33, 34}
EXITS_USE, EXITS_WALK = {11, 51}, {52, 124}
CELL = 16
# solid decorations (doomednum: radius), from the Doom thing table
SOLID = {2035: 10, 70: 16, 2028: 16, 25: 16, 26: 16, 27: 16, 28: 16, 29: 16, 30: 16, 31: 16,
         32: 16, 33: 16, 35: 16, 36: 16, 37: 16, 41: 16, 42: 16, 43: 16, 44: 16, 45: 16, 46: 16,
         47: 16, 48: 16, 54: 32, 55: 16, 56: 16, 57: 16, 85: 16, 86: 16}


def jag_lumps(path):
    d = open(path, "rb").read()
    n, ofs = struct.unpack(">II", d[4:12])
    out = []
    for i in range(n):
        pos, size, name = struct.unpack(">II8s", d[ofs + 16 * i:ofs + 16 * i + 16])
        comp = name[0] & 0x80
        nm = (bytes([name[0] & 0x7F]) + name[1:]).split(b"\0")[0].decode("latin-1")
        if comp and size:
            data = lzss_decode(d[pos:])[:size]
        else:
            data = d[pos:pos + size]
        out.append((nm, data))
    return out


class Level:
    def __init__(self, wad, mapnum):
        lumps = jag_lumps(wad)
        i = [n for n, _ in lumps].index(f"MAP{mapnum:02d}")
        L = {n: d for n, d in lumps[i + 1:i + 11]}
        self.v = [struct.unpack("<hh", L["VERTEXES"][k:k + 4]) for k in range(0, len(L["VERTEXES"]), 4)]
        self.lines = [struct.unpack("<7h", L["LINEDEFS"][k:k + 14]) for k in range(0, len(L["LINEDEFS"]), 14)]
        self.sides = [struct.unpack("<hh24xh", L["SIDEDEFS"][k:k + 30])[2] for k in range(0, len(L["SIDEDEFS"]), 30)]
        self.sectors = [struct.unpack("<hh16xhhh", L["SECTORS"][k:k + 26]) for k in range(0, len(L["SECTORS"]), 26)]
        self.ssec = [struct.unpack("<hh", L["SSECTORS"][k:k + 4]) for k in range(0, len(L["SSECTORS"]), 4)]
        self.segs = [struct.unpack("<6h", L["SEGS"][k:k + 12]) for k in range(0, len(L["SEGS"]), 12)]
        self.nodes = [struct.unpack("<4h8h2H", L["NODES"][k:k + 28]) for k in range(0, len(L["NODES"]), 28)]
        self.things = [struct.unpack("<5h", L["THINGS"][k:k + 10]) for k in range(0, len(L["THINGS"]), 10)]
        self.solid = [(t[0], t[1], SOLID[t[3]]) for t in self.things if t[3] in SOLID]
        self.buckets = {}
        for li, ln in enumerate(self.lines):
            (x1, y1), (x2, y2) = self.v[ln[0]], self.v[ln[1]]
            for bx in range(min(x1, x2) // 128, max(x1, x2) // 128 + 1):
                for by in range(min(y1, y2) // 128, max(y1, y2) // 128 + 1):
                    self.buckets.setdefault((bx, by), []).append(li)

    def sector_at(self, x, y):
        if not self.nodes:
            return self.segsector(self.ssec[0][1])
        n = len(self.nodes) - 1
        while True:
            nx, ny, ndx, ndy = self.nodes[n][:4]
            left = ndy * (x - nx)
            right = (y - ny) * ndx
            child = self.nodes[n][12 + (0 if right < left else 1)]
            if child & 0x8000:
                return self.segsector(self.ssec[child & 0x7FFF][1])
            n = child

    def segsector(self, seg):
        _v1, _v2, _a, li, side, _o = self.segs[seg]
        sd = self.lines[li][5 + side]
        return self.sides[sd]

    def line_sectors(self, li):
        ln = self.lines[li]
        f = self.sides[ln[5]] if ln[5] >= 0 else None
        b = self.sides[ln[6]] if ln[6] >= 0 else None
        return f, b

    def near_lines(self, x0, y0, x1, y1):
        seen = set()
        for bx in range(int(min(x0, x1)) // 128 - 1, int(max(x0, x1)) // 128 + 2):
            for by in range(int(min(y0, y1)) // 128 - 1, int(max(y0, y1)) // 128 + 2):
                for li in self.buckets.get((bx, by), ()):
                    if li not in seen:
                        seen.add(li)
                        yield li

    def passable(self, li, s_from, s_to):
        ln = self.lines[li]
        if ln[6] < 0 or ln[2] & 1:
            return False
        if ln[3] in DOORS:
            return ln[3] not in LOCKED
        f1, c1 = self.sectors[s_from][:2]
        f2, c2 = self.sectors[s_to][:2]
        c1, c2 = self.door_ceiling(s_from, c1), self.door_ceiling(s_to, c2)
        if f2 - f1 > 24:
            return False
        return min(c1, c2) - max(f1, f2) >= 56

    def door_ceiling(self, s, ceil):
        """a closed door's ceiling as it will be once the door is open:
        just below its lowest neighbouring ceiling, like EV_DoDoor"""
        if not self.door_sector(s):
            return ceil
        if not hasattr(self, "_doorceil"):
            self._doorceil = {}
        if s not in self._doorceil:
            around = []
            for li, ln in enumerate(self.lines):
                f, b = self.line_sectors(li)
                if b is not None and s in (f, b):
                    around.append(self.sectors[b if f == s else f][1])
            self._doorceil[s] = max(ceil, min(around) - 4) if around else ceil
        return self._doorceil[s]

    @staticmethod
    def crosses(ax, ay, bx, by, cx, cy, dx, dy):
        def o(px, py, qx, qy, rx, ry):
            return (qx - px) * (ry - py) - (qy - py) * (rx - px)
        d1, d2 = o(cx, cy, dx, dy, ax, ay), o(cx, cy, dx, dy, bx, by)
        d3, d4 = o(ax, ay, bx, by, cx, cy), o(ax, ay, bx, by, dx, dy)
        return (d1 > 0) != (d2 > 0) and (d3 > 0) != (d4 > 0)

    def can_move(self, ax, ay, bx, by):
        sa = self.sector_at(ax, ay)
        for li in self.near_lines(ax, ay, bx, by):
            ln = self.lines[li]
            (x1, y1), (x2, y2) = self.v[ln[0]], self.v[ln[1]]
            if self.crosses(ax, ay, bx, by, x1, y1, x2, y2):
                f, b = self.line_sectors(li)
                if b is None:
                    return False
                other = b if f == sa else f
                if not self.passable(li, sa, other):
                    return False
                sa = other
        return True

    def blocker(self, li):
        """does this line stop a player crossing it either way?"""
        if not hasattr(self, "_block"):
            self._block = {}
        b = self._block.get(li)
        if b is None:
            ln = self.lines[li]
            f, k = self.line_sectors(li)
            if k is None or ln[2] & 1:
                b = True
            elif ln[3] in DOORS:
                b = ln[3] in LOCKED
            else:
                b = not (self.passable(li, f, k) and self.passable(li, k, f))
            self._block[li] = b
        return b

    def clear_path(self, ax, ay, bx, by, r=20):
        n = max(1, int(math.hypot(bx - ax, by - ay) // 12))
        return all(self.clear(ax + (bx - ax) * i / n, ay + (by - ay) * i / n, r) for i in range(n + 1))

    def clear(self, x, y, r=19):
        for sx, sy, sr in self.solid:
            if abs(sx - x) < sr + r and abs(sy - y) < sr + r:
                return False
        for li in self.near_lines(x - r, y - r, x + r, y + r):
            ln = self.lines[li]
            if not self.blocker(li):
                continue
            (x1, y1), (x2, y2) = self.v[ln[0]], self.v[ln[1]]
            if self.seg_in_box(x1, y1, x2, y2, x - r, y - r, x + r, y + r):
                return False
        return True

    @staticmethod
    def seg_in_box(x1, y1, x2, y2, bx0, by0, bx1, by1):
        """does the segment touch the box?  (Doom collides with a box,
        not a circle: P_CheckPosition uses the thing's bounding box)"""
        t0, t1 = 0.0, 1.0
        dx, dy = x2 - x1, y2 - y1
        for p, q in ((-dx, x1 - bx0), (dx, bx1 - x1), (-dy, y1 - by0), (dy, by1 - y1)):
            if p == 0:
                if q < 0:
                    return False
            else:
                t = q / p
                if p < 0:
                    t0 = max(t0, t)
                else:
                    t1 = min(t1, t)
                if t0 > t1:
                    return False
        return True

    def route(self, start, goals):
        """A* on the cell grid from start (x, y) to any goal cell."""
        def cell(p):
            return (int(round(p[0] / CELL)), int(round(p[1] / CELL)))
        sc = cell(start)
        goal = {cell(g) for g in goals}
        gx = sum(c[0] for c in goal) / len(goal)
        gy = sum(c[1] for c in goal) / len(goal)
        ok = {}

        def walkable(c):
            if c not in ok:
                x, y = c[0] * CELL, c[1] * CELL
                s = self.sector_at(x, y)
                f, ce, _l, special, _t = self.sectors[s]
                ok[c] = (ce - f >= 56 or self.door_sector(s)) and self.clear(x, y)
                ok[c] = (ok[c], 40 if special in (4, 5, 7, 16) else 0)
            return ok[c]
        openq = [(0, 0, sc)]
        came = {sc: None}
        cost = {sc: 0}
        steps = 0
        while openq:
            _, g, c = heapq.heappop(openq)
            if c in goal:
                path = []
                while c:
                    path.append((c[0] * CELL, c[1] * CELL))
                    c = came[c]
                return path[::-1]
            steps += 1
            if steps > 400000:
                break
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)):
                n = (c[0] + dx, c[1] + dy)
                w, pen = walkable(n)
                if not w and n not in goal:
                    continue
                if not self.can_move(c[0] * CELL, c[1] * CELL, n[0] * CELL, n[1] * CELL):
                    continue
                ng = g + (1.414 if dx and dy else 1) + pen
                if ng < cost.get(n, 1e9):
                    cost[n] = ng
                    came[n] = c
                    heapq.heappush(openq, (ng + math.hypot(n[0] - gx, n[1] - gy), ng, n))
        return None

    def door_sector(self, s):
        if not hasattr(self, "_doors"):
            self._doors = {self.sides[ln[6]] for ln in self.lines if ln[3] in DOORS and ln[6] >= 0}
        return s in self._doors

    def exits(self):
        """-> list of (goal points, needs use, facing point)"""
        out = []
        for li, ln in enumerate(self.lines):
            if ln[3] in EXITS_USE | EXITS_WALK:
                (x1, y1), (x2, y2) = self.v[ln[0]], self.v[ln[1]]
                mx, my = (x1 + x2) / 2, (y1 + y2) / 2
                nx, ny = (y2 - y1), -(x2 - x1)                    # front side normal
                k = math.hypot(nx, ny) or 1
                pts = [(mx + nx / k * d, my + ny / k * d) for d in (24, 32, 40)]
                out.append((pts, ln[3] in EXITS_USE, (mx, my)))
        return out


class Pilot:
    def __init__(self, emu, outdir):
        self.e = emu
        self.out = outdir
        self.P = emu.syms["_players"]
        self.head = emu.syms["_mobjhead"]
        self.shots = 0
        self.goals = None
        self.fighting = 0
        self.calm_until = 0

    def state(self):
        mo = self.e.u32(self.P)
        if not mo:
            return None
        d = self.e.peek(mo + MO_X, 8)
        x, y = struct.unpack(">ii", d)
        a = self.e.u32(mo + MO_ANGLE)
        return x / 65536, y / 65536, a * 2 * math.pi / 2 ** 32, mo

    def monsters(self, x, y):
        out = []
        m = self.e.u32(self.head + 4)
        n = 0
        while m and m != self.head and n < 400:
            d = self.e.peek(m, 108)
            nxt = struct.unpack(">I", d[4:8])[0]
            flags, health = struct.unpack(">ii", d[MO_FLAGS:MO_FLAGS + 8])
            if flags & MF_COUNTKILL and flags & MF_SHOOTABLE and health > 0:
                mx, my = struct.unpack(">ii", d[MO_X:MO_X + 8])
                mx, my = mx / 65536, my / 65536
                out.append((math.hypot(mx - x, my - y), mx, my))
            m = nxt
            n += 1
        return sorted(out)

    def shot(self, tag):
        self.shots += 1
        p = os.path.join(self.out, f"{self.shots:03d}-{tag}.png")
        self.e.shot(p)
        return p

    def use_line(self, face, tag):
        """Line up in front of a switch, face it, step in, press Use.  The
        Jaguar's P_UseLines takes the nearest line the use trace's bounding
        box overlaps (it never checks the hit lies on the segment), so a
        switch in an alcove must be approached head-on."""
        e = self.e
        front = self.goals[-1] if self.goals else face
        for _ in range(400):                       # walk to the spot in front
            st = self.state()
            if st is None:
                return True
            x, y, a, _ = st
            if math.hypot(front[0] - x, front[1] - y) < 6:
                break
            want = math.atan2(front[1] - y, front[0] - x)
            diff = (want - a + math.pi) % (2 * math.pi) - math.pi
            if os.environ.get("TRACE") and _ % 20 == 0:
                print(f"use_line: at ({x:.0f},{y:.0f}) a {math.degrees(a):.0f} -> front {front} diff {diff:.2f}")
            e.run(2, ("L" if diff > 0 else "R") if abs(diff) > 0.2 else "U")
        for _ in range(300):
            st = self.state()
            if st is None:
                return True
            x, y, a, _ = st
            want = math.atan2(face[1] - y, face[0] - x)
            diff = (want - a + math.pi) % (2 * math.pi) - math.pi
            if abs(diff) > 0.05:
                e.run(1, "L" if diff > 0 else "R")
                e.run(1)
                continue
            if math.hypot(face[0] - x, face[1] - y) > 30:
                e.run(1, "U")
                e.run(1)
                continue
            self.shot(tag + "-exit")
            for _ in range(3):
                before = e.sym32("_ticon")
                e.run(6, "C")
                e.run(12)
                # a new MiniLoop (the intermission) restarts ticon; any more
                # presses would skip it
                if e.sym32("_gameaction") or e.sym32("_ticon") < before:
                    return True
            return True
        return False

    def skirmish(self, level, frames, every, tag):
        """stand and fight: face the nearest visible monster and shoot,
        otherwise look around (the gunfire wakes the neighbourhood)"""
        e = self.e
        t0 = e.frame
        while e.frame - t0 < frames:
            st = self.state()
            if st is None:
                e.run(4)
                continue
            x, y, a, mo = st
            if (e.frame - t0) % every < 2:
                self.shot(tag)
            mons = [m for m in self.monsters(x, y) if m[0] < 1200 and level.can_see(x, y, m[1], m[2])]
            if not mons:
                e.run(2, "L" if (e.frame // 240) % 2 else "B")
                continue
            _d, mx, my = mons[0]
            want = math.atan2(my - y, mx - x)
            diff = (want - a + math.pi) % (2 * math.pi) - math.pi
            b = ["B"] if abs(diff) < 0.15 else []
            if abs(diff) > 0.06:
                b.append("L" if diff > 0 else "R")
            e.run(2, b)

    def drive(self, level, route, frames, use_at_end=False, face=None, shoot=True, every=300, tag="play"):
        e = self.e
        wp = 0
        stuck = 0
        hist = []
        t0 = e.frame
        log = []
        while e.frame - t0 < frames:
            st = self.state()
            if st is None:
                e.run(2)
                continue
            x, y, a, mo = st
            if e.s32(mo + MO_HEALTH) <= 0:            # dead: B restarts the level
                self.shot(tag + "-dead")
                e.run(60)
                e.run(4, "B")
                e.run(120)
                continue
            if (e.frame - t0) % every < 2:
                self.shot(tag)
            buttons = []
            target = None
            if shoot and (e.frame // 2) % 3 == 0:
                mons = self.monsters(x, y)
                self.close_mons = [m for m in mons if m[0] < 700 and level.can_see(x, y, m[1], m[2])]
            mons = getattr(self, "close_mons", [])
            # fight for at most 10 s at a time, then push on for 8 s
            if mons and e.frame >= self.calm_until:
                self.fighting += 2
                if self.fighting > 600:
                    self.calm_until, self.fighting = e.frame + 480, 0
            else:
                self.fighting = 0
            if e.frame < self.calm_until:
                mons = []
            if mons:
                _d, mx, my = mons[0]
                target = (mx, my)
            else:
                # follow the nearest route point ahead of the last one reached
                window = range(wp, min(len(route), wp + 40))
                wp = min(window, key=lambda i: math.hypot(route[i][0] - x, route[i][1] - y))
                if wp < len(route) - 1 and math.hypot(route[wp][0] - x, route[wp][1] - y) < 24:
                    wp += 1
                # look ahead along the route while the way is clear
                k = wp
                while (k + 1 < len(route) and k < wp + 8 and level.can_move(x, y, *route[k + 1])
                       and level.clear_path(x, y, *route[k + 1], r=17)):
                    k += 1
                target = route[k]
                if wp >= len(route) - 1 and math.hypot(route[-1][0] - x, route[-1][1] - y) < 28:
                    if face:
                        return self.use_line(face, tag)
                    return True
            want = math.atan2(target[1] - y, target[0] - x)
            diff = (want - a + math.pi) % (2 * math.pi) - math.pi
            if diff > 0.5:
                buttons.append("L")
            elif diff < -0.5:
                buttons.append("R")
            elif diff > 0.08:
                buttons += ["L", "U"]
            elif diff < -0.08:
                buttons += ["R", "U"]
            else:
                buttons.append("U")
            if mons:
                if "U" in buttons and mons[0][0] < 200:
                    buttons.remove("U")
                if abs(diff) < 0.12:
                    buttons.append("B")
            elif "U" in buttons and abs(diff) < 0.3:
                buttons.append("A")
            # stuck: little progress over the last 40 frames while walking
            hist.append((x, y))
            del hist[:-20]
            if "U" in buttons and len(hist) == 20 and math.hypot(x - hist[0][0], y - hist[0][1]) < 12:
                stuck += 1
                hist.clear()
                e.run(6, "C")                      # a door?  press Use once, then carry on
                e.run(10)
                if stuck > 2:                      # back off and plan again from here
                    e.run(10, "D")
                    st = self.state()
                    if st and self.goals:
                        r = level.route(st[:2], self.goals)
                        if os.environ.get("TRACE"):
                            print(f"f{e.frame} stuck at ({st[0]:.0f},{st[1]:.0f}); new route {r and len(r)} "
                                  f"via {r and r[:6]}")
                        if r:
                            route, wp = r, 0
                    stuck = 0
                continue
            elif "U" in buttons and len(hist) == 20 and math.hypot(x - hist[0][0], y - hist[0][1]) > 40:
                stuck = 0
            e.run(2, buttons)
            if os.environ.get("TRACE") and (e.frame - t0) % 60 < 2:
                print(f"f{e.frame} pos ({x:.0f},{y:.0f}) a {math.degrees(a):.0f} wp {wp}/{len(route)} "
                      f"target ({target[0]:.0f},{target[1]:.0f}) mons {[(round(m[0]), round(m[1]), round(m[2])) for m in mons[:2]]} "
                      f"{' '.join(buttons)} stuck {stuck}")
            log.append((e.frame, round(x), round(y)))
        return False


def can_see(level, x0, y0, x1, y1):
    for li in level.near_lines(x0, y0, x1, y1):
        ln = level.lines[li]
        (ax, ay), (bx, by) = level.v[ln[0]], level.v[ln[1]]
        if level.crosses(x0, y0, x1, y1, ax, ay, bx, by):
            if ln[6] < 0:
                return False
            f, b = level.line_sectors(li)
            ff, fc = level.sectors[f][:2]
            bf, bc = level.sectors[b][:2]
            if min(fc, bc) - max(ff, bf) <= 0:
                return False
    return True


Level.can_see = can_see


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("wad")
    ap.add_argument("rom")
    ap.add_argument("out")
    ap.add_argument("--map", type=int, default=1)
    ap.add_argument("--seconds", type=int, default=120)
    ap.add_argument("--every", type=int, default=240)
    ap.add_argument("--god", action="store_true", help="set the god mode cheat flag (test driving)")
    ap.add_argument("--record", help="record the run with the game's demo recorder into this file")
    ap.add_argument("--skill", type=int, help="0-4 (the menu's default is 2, Hurt me plenty)")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    level = Level(a.wad, a.map)
    e = Emu(os.path.abspath(a.rom))
    e.wav(os.path.join(a.out, "audio.wav"))
    pilot = Pilot(e, a.out)
    e.run(150)
    pilot.shot("title")
    e.run(4, "B")
    e.run(40)
    pilot.shot("menu")
    if a.map != 1:
        # allow every area (as if the EEPROM said so) and pick it in the menu
        e.poke(e.syms["_maxlevel"], (24).to_bytes(4, "big"))
        e.poke(e.syms["_playermap"], a.map.to_bytes(4, "big"))
        e.run(30)
        pilot.shot("menu-area")
    if a.skill is not None:
        e.poke(e.syms["_playerskill"], a.skill.to_bytes(4, "big"))
        e.run(30)
        pilot.shot("menu-skill")
    e.run(4, "B")
    if a.record:
        # G_InitNew clears demorecording; set it (and a scratch buffer: the
        # 7936 bytes between screens[0]'s 160x180 frame and screens[1]) while
        # the level loads, so every tic is the fixed 4 vblanks a demo uses
        emptymobj = e.syms["_emptymobj"]
        while e.u32(e.syms["_players"]) != emptymobj:
            e.run(1)
        e.poke(e.syms["_demo_p"], DEMOBUF.to_bytes(4, "big"))
        e.poke(e.syms["_demorecording"], (1).to_bytes(4, "big"))
    e.run(4)
    while True:                                 # wait for the level to load
        st = pilot.state()
        if st and (st[0], st[1]) != (0, 0):
            break
        e.run(10)
    e.run(30)
    if a.god:
        cheats = e.syms["_players"] + PL_CHEATS
        e.poke(cheats, (e.u32(cheats) | 2).to_bytes(4, "big"))
    x, y, _a, _ = pilot.state()
    ex = level.exits()
    print(f"MAP{a.map:02d}: start ({x:.0f},{y:.0f}); {len(ex)} exit lines")
    best = None
    for pts, use, face in ex:
        r = level.route((x, y), pts)
        if r and (best is None or len(r) < len(best[0])):
            best = (r, use, face)
    if not best:
        # no way to the exit without keys/lifts/teleporters: go and meet the
        # monsters instead, spectres first
        for kinds in ((58,), (3002, 3001, 3004, 9, 3005, 3003)):
            cands = sorted((math.hypot(t[0] - x, t[1] - y), t) for t in level.things if t[3] in kinds)
            for _d, t in cands[:6]:
                r = level.route((x, y), [(t[0] + dx, t[1] + dy) for dx in (-32, 0, 32) for dy in (-32, 0, 32)])
                if r:
                    best = (r, False, None)
                    print(f"no route to an exit; heading for a thing of type {t[3]} at ({t[0]},{t[1]})")
                    break
            if best:
                break
    if not best:
        print("no route to an exit; wandering")
        best = ([(x, y)], False, None)
    route, use, face = best
    pilot.goals = next((pts for pts, u, f in ex if f == face), None)
    print(f"route: {len(route)} cells ({len(route) * CELL} units), exit by {'use' if use else 'walking'}")
    t0 = e.frame
    done = pilot.drive(level, route, a.seconds * 60, use_at_end=use, face=face if use else None,
                       every=a.every, tag=f"map{a.map:02d}")
    if pilot.goals is None and a.seconds * 60 > e.frame - t0:      # went to meet monsters
        pilot.skirmish(level, a.seconds * 60 - (e.frame - t0), a.every, f"map{a.map:02d}")
        done = False
    print("reached the exit" if done else "did not reach the exit in time")
    if a.record:
        end = e.u32(e.syms["_demo_p"])
        cmds = e.peek(DEMOBUF, end - DEMOBUF)
        e.poke(e.syms["_demorecording"], bytes(4))
        skill = e.sym32("_gameskill")
        lump = struct.pack(">II", skill, a.map) + cmds + struct.pack(">I", 0x10000000)   # BT_PAUSE ends it
        open(a.record, "wb").write(lump)
        print(f"recorded {len(cmds) // 4} tics ({len(cmds) / 4 / 15:.0f} s) to {a.record}")
    for i in range(14):                         # intermission, then the next level
        e.run(30)
        if i in (3, 6):
            pilot.shot("after")
        if i in (7, 10):
            e.run(4, "B")
    e.wav("-")
    st = pilot.state()
    print("final", st and tuple(round(v, 1) for v in st[:3]), "gamemap", e.sym32("_gamemap"))
    e.close()


if __name__ == "__main__":
    main()
