"""The thumbnail draws the page's field, so the two must not drift apart.

``tools/thumbnail.py`` reimplements the hero canvas's seeded generator in
Python in order to render ``docs/og.png``. Nothing forces the two to agree --
an edit to the JavaScript in ``docs/index.html`` would quietly change the page
and leave the thumbnail drawing last month's wall, and the failure is invisible
because both pictures still look like plausible fields.

So: pull the generator out of the page, run it under node, and compare the
actual numbers. The parity check is skipped when node is absent, but the
constants check is not -- that one is a plain read of the page's source.
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.thumbnail import (  # noqa: E402
    FINDINGS,
    MEMBERS,
    SEED_BARS,
    SEED_IDS,
    SEED_MARKS,
    build_bars,
    build_marks,
    mulberry32,
)

ok = lambda m: print(f"[ok] {m}")

PAGE = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")

# --- the page still says what the thumbnail assumes ------------------------
# Each of these is a number tools/thumbnail.py hard-codes. If the page changes
# one, the thumbnail is drawing a different wall than the hero above it.
ASSUMPTIONS = {
    "member count": f"MEMBERS = {MEMBERS}",
    "finding count": f"FINDINGS = {FINDINGS}",
    "bar seed": f"mulberry32(0x{SEED_BARS:X})",
    "mark seed": f"mulberry32(0x{SEED_MARKS:X})",
    "id-rail seed": f"mulberry32(0x{SEED_IDS:X})",
    "one bar per ~14 members": "MEMBERS / 14",
    "density envelope": "Math.pow(Math.abs(x - 0.5) * 2, 1.5)",
    "bar width roll": "rand() < 0.14 ? 2 : 1",
    "bar alpha roll": "(0.03 + Math.pow(rand(), 2.4) * 0.26) * (0.16 + env * 0.84)",
    "mark placement": "0.04 + mrand() * 0.92",
}
for label, needle in ASSUMPTIONS.items():
    assert needle in PAGE, f"docs/index.html no longer contains the {label}: {needle!r}"
ok(f"page still declares all {len(ASSUMPTIONS)} constants the thumbnail assumes")

# --- the generator itself agrees, value for value -------------------------
SOURCE = re.search(
    r"function mulberry32\(a\) \{.*?\n  \}\n", PAGE, re.S
)
assert SOURCE, "could not find mulberry32 in docs/index.html"

node = shutil.which("node")
if not node:
    print("[skip] node not installed -- generator parity not checked")
else:
    SEEDS = {"bars": SEED_BARS, "marks": SEED_MARKS, "ids": SEED_IDS}
    harness = SOURCE.group(0) + (
        "\nconst out = {};\n"
        f"for (const [name, seed] of Object.entries({json.dumps(SEEDS)})) {{\n"
        "  const r = mulberry32(seed);\n"
        "  out[name] = Array.from({length: 500}, () => r());\n"
        "}\n"
        "console.log(JSON.stringify(out));\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as handle:
        handle.write(harness)
        script = handle.name
    try:
        raw = subprocess.run(
            [node, script], capture_output=True, text=True, check=True, timeout=60
        ).stdout
    finally:
        Path(script).unlink(missing_ok=True)

    reference = json.loads(raw)
    worst = 0.0
    for name, seed in SEEDS.items():
        rand = mulberry32(seed)
        for index, expected in enumerate(reference[name]):
            got = rand()
            assert abs(got - expected) < 1e-12, (
                f"{name} diverges at draw {index}: page {expected!r}, python {got!r}"
            )
            worst = max(worst, abs(got - expected))
    ok(f"mulberry32 matches the page across {len(SEEDS)} seeds x 500 draws "
       f"(worst delta {worst:.2e})")

# --- and the shapes built from it are the right size ----------------------
bars = build_bars()
assert len(bars) == max(120, round(MEMBERS / 14)) == 774, len(bars)
assert all(0.0 <= x < 1.0 for x, _, _ in bars)
assert all(width in (1, 2) for _, width, _ in bars)
assert all(0.0 <= alpha <= 1.0 for _, _, alpha in bars)
# the envelope is the whole point: edges dense, middle thin
edge = sum(a for x, _, a in bars if x < 0.06 or x > 0.94)
middle = sum(a for x, _, a in bars if 0.47 < x < 0.53)
assert edge > middle * 4, f"envelope is flat: edge {edge:.2f} vs middle {middle:.2f}"
ok(f"{len(bars)} bars, edges {edge / middle:.1f}x denser than the middle")

marks = build_marks()
assert len(marks) == FINDINGS
assert marks == sorted(marks, key=lambda m: m[0]), "marks must run left to right"
assert all(0.04 <= x <= 0.96 for x, _ in marks)
ok(f"{len(marks)} findings, sorted across the wall")

print("\nall thumbnail checks passed.")
