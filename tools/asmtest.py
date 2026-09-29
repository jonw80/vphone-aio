#!/usr/bin/env python3
"""
asmtest.py - assemble a GPU/DSP overlay for functional testing.

Every `.extern` is replaced by an absolute test address, so rmac produces a
fully resolved image with no relocations.  `overrides` rewrites `NAME .equ
value` lines in included files (e.g. FASTCOLUMNS) by shadowing the include
in a scratch directory.
"""
import os
import re
import struct
import subprocess
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RMAC = os.environ.get("RMAC", os.path.join(ROOT, "tools", "bin", "rmac"))


def assemble(src, entry, syms, overrides=None, auto_base=0x1C0000):
    text = open(os.path.join(ROOT, src), "rb").read().decode("latin-1")
    externs = re.findall(r"^[ \t]*\.extern[ \t]+([^\n;]+)", text, flags=re.M)
    names = [n.strip() for line in externs for n in line.split(",") if n.strip()]
    text = re.sub(r"^[ \t]*\.extern[^\n]*\n", "", text, flags=re.M)
    defined = set(re.findall(r"^([A-Za-z_][A-Za-z0-9_]*)::?", text, flags=re.M))
    names = [n for n in names if n not in defined]
    table = dict(syms)
    for n in names:
        if n not in table:
            table[n] = auto_base
            auto_base += 0x100
    eq = "".join(f"{k}\t.equ\t${v:X}\n" for k, v in table.items() if n_in(k, text))
    with tempfile.TemporaryDirectory() as td:
        for inc in re.findall(r'\.include\s+"([^"]+)"', text):
            body = open(os.path.join(ROOT, inc), "rb").read().decode("latin-1")
            for k, v in (overrides or {}).items():
                body, n = re.subn(rf"^({k}\s+\.equ\s+)\S+", rf"\g<1>{v}", body, flags=re.M)
                assert n == 1, f"{k} not found in {inc}"
            open(os.path.join(td, inc), "wb").write(body.encode("latin-1"))
        s, o = os.path.join(td, "t.gas"), os.path.join(td, "t.o")
        open(s, "wb").write((eq + text).encode("latin-1"))
        out = subprocess.run([RMAC, "-fb", "t.gas", "-o", "t.o"], cwd=td, capture_output=True, text=True)
        assert "rror" not in out.stdout + out.stderr, out.stdout + out.stderr
        d = open(o, "rb").read()
    _m, tsz, dsz, _b, ssz, _e, trs, drs = struct.unpack(">8I", d[:32])
    assert trs == 0 and drs == 0, f"{src}: unexpected relocations"
    tx = d[32:32 + tsz]
    so, st = 32 + tsz + dsz, 32 + tsz + dsz + ssz
    symbols = {}
    for i in range(so, st, 12):
        sx, _t, val = struct.unpack(">IIi", d[i:i + 12])
        e = d.index(b"\0", st + sx)
        symbols[d[st + sx:e].decode()] = val
    return tx[symbols[entry]:symbols["codeend"]], symbols, table


def n_in(name, text):
    return re.search(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])", text) is not None
