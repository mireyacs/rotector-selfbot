"""The project page: the canvas, the platform picker, and the favicon.

The canvas half exists because of a real regression. ``resize()`` redrew the
field at progress 0, and at 0 every finding is skipped (``mk.x > p`` is true for
all fourteen), the sine loop runs zero iterations (``sx <= w * 0``) and the bars
drop to 22% alpha -- so dragging the window erased the marks and the wave, and
nothing ever brought them back because the intro animation only runs once.

The page's JavaScript is run under node here rather than reasoned about, since
that is the only way to tell the difference between "looks right" and "draws
fourteen squares".
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "docs" / "index.html"
sys.path.insert(0, str(ROOT))

ok = lambda m: print(f"[ok] {m}")

HTML = PAGE.read_text(encoding="utf-8")
SCRIPT = HTML[HTML.rindex("<script>") + 8: HTML.rindex("</script>")]

NODE = shutil.which("node")


def _node(source: str):
    """Run a snippet under node and parse the JSON it prints."""
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as handle:
        handle.write(source)
        path = handle.name
    try:
        out = subprocess.run([NODE, path], capture_output=True, text=True,
                             timeout=60, check=True).stdout
    finally:
        Path(path).unlink(missing_ok=True)
    return json.loads(out)


# --- the favicon is drawn, present, and linked ----------------------------
for name in ("favicon.svg", "favicon.ico", "apple-touch-icon.png"):
    asset = ROOT / "docs" / name
    assert asset.is_file(), f"{name} is missing; run tools/favicon.py"
    assert asset.stat().st_size > 0, name
    assert f'href="{name}"' in HTML, f"{name} exists but the page does not link it"
ok("favicon.svg, favicon.ico and apple-touch-icon.png exist and are linked")

favicon = (ROOT / "docs" / "favicon.svg").read_text(encoding="utf-8")
assert 'fill="#000000"' in favicon and 'fill="#ffffff"' in favicon, (
    "the mark must be the page's two values"
)
assert "viewBox=\"0 0 16 16\"" in favicon, "the grid is what keeps it crisp at 16px"
ok("the mark uses the page's two values on a 16-unit grid")


# --- the platform picker --------------------------------------------------
tabs = re.findall(r'role="tab"[^>]*data-os="([a-z]+)"', HTML)
assert tabs == ["linux", "macos", "windows"], tabs
panes = re.findall(r'class="os-pane"[^>]*data-os="([^"]+)"', HTML)
assert len(panes) >= 6, panes
for platform in ("linux", "macos", "windows"):
    covering = [p for p in panes if platform in p.split()]
    assert len(covering) == 3, f"{platform} has {len(covering)} panes, expected 3"
ok(f"all three platforms are offered, each covered by 3 of the {len(panes)} panes")

# the commands really do differ, or the picker would be theatre
assert "py -3.11 -m venv" in HTML and r".venv\Scripts\pip" in HTML
assert "python3 -m venv .venv" in HTML and "./.venv/bin/pip" in HTML
assert "copy config.example.toml" in HTML and "cp config.example.toml" in HTML
ok("Windows gets py/Scripts/copy where the others get python3/bin/cp")

# without scripting every platform stays on the page, labelled
assert ".js .os-pane[hidden] { display: none; }" in HTML, (
    "panes must only be hidden once the picker is running"
)
assert "os-pane__name" in HTML, "each pane needs a heading for the no-JS case"
ok("with no JavaScript all three stay visible and labelled")


if NODE is None:
    print("[skip] node not installed -- page scripts not executed")
else:
    # --- detection, against agents that actually exist --------------------
    body = SCRIPT[SCRIPT.index("function detect()"):SCRIPT.index("function show(")]
    cases = {
        "uach-windows": ({"userAgentData": {"platform": "Windows"}}, "windows"),
        "uach-macos": ({"userAgentData": {"platform": "macOS"}}, "macos"),
        "uach-linux": ({"userAgentData": {"platform": "Linux"}}, "linux"),
        "win32": ({"platform": "Win32"}, "windows"),
        "macintel": ({"platform": "MacIntel"}, "macos"),
        "iphone": ({"userAgent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 "
                                 "like Mac OS X)"}, "macos"),
        "android": ({"userAgent": "Mozilla/5.0 (Linux; Android 14) Chrome"}, "linux"),
        "windows-ua": ({"userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                       "windows"),
        "freebsd": ({"userAgent": "Mozilla/5.0 (X11; FreeBSD amd64)"}, "linux"),
        # a spoofed or stripped agent must still land somewhere usable, which
        # is the reason the picker is always visible
        "nonsense": ({"userAgent": "TotallyNotABrowser/1.0"}, "linux"),
        "empty": ({}, "linux"),
    }
    harness = (
        f"const detect = new Function('navigator', {json.dumps(body)} + "
        f"'; return detect();');\n"
        f"const cases = {json.dumps({k: v[0] for k, v in cases.items()})};\n"
        "const out = {};\n"
        "for (const k in cases) out[k] = detect(cases[k]);\n"
        "console.log(JSON.stringify(out));\n"
    )
    got = _node(harness)
    for name, (_, want) in cases.items():
        assert got[name] == want, f"{name}: detected {got[name]}, expected {want}"
    ok(f"all {len(cases)} user agents resolve to the right platform")

    # --- the canvas survives a resize -------------------------------------
    harness = r"""
const fs = require('fs');
const html = fs.readFileSync(PAGE, 'utf8');
const script = html.slice(html.lastIndexOf('<script>') + 8, html.lastIndexOf('</script>'));
let ops = { rects: [], pts: 0 };
const ctx = { fillStyle:'', strokeStyle:'', globalAlpha:1, lineWidth:1,
  setTransform(){}, fillRect(x,y,w,h){ ops.rects.push({w,h,a:this.globalAlpha}); },
  beginPath(){}, moveTo(){ ops.pts++; }, lineTo(){ ops.pts++; }, stroke(){} };
let width = 1400;
const canvas = { getContext:()=>ctx, width:0, height:0,
                 getBoundingClientRect:()=>({width, height:900}) };
const L = {};
global.window = { devicePixelRatio:1,
  matchMedia:()=>({matches:false, addEventListener(){}}),
  addEventListener:(n,f)=>{L[n]=f;} };
global.document = { getElementById:id=>(id==='field'?canvas:null),
  querySelector:()=>null, querySelectorAll:()=>[], addEventListener(){},
  documentElement:{className:''} };
let rafFn = null;
global.requestAnimationFrame = fn => { rafFn = fn; return 1; };
global.cancelAnimationFrame = ()=>{};
eval(script);
// let the one authored motion finish
if (rafFn) { rafFn(0); while (rafFn) { const f = rafFn; rafFn = null; f(3000); } }
function frame() {
  ops = { rects: [], pts: 0 };
  L.resize();
  if (rafFn) { const f = rafFn; rafFn = null; f(0); }
  return { findings: ops.rects.filter(r => r.w === 9 && r.h === 9).length,
           sine: ops.pts,
           bright: ops.rects.filter(r => r.w <= 2 && r.a > 0.05).length };
}
width = 900;  const narrow = frame();
width = 1600; const wide = frame();
console.log(JSON.stringify({narrow, wide}));
"""
    harness = harness.replace("PAGE", json.dumps(str(PAGE)))
    result = _node(harness)
    for label, drawn in result.items():
        assert drawn["findings"] == 14, (
            f"{label}: {drawn['findings']} findings after a resize, expected 14 -- "
            f"the field is being redrawn at progress 0 again"
        )
        assert drawn["sine"] > 100, (
            f"{label}: the sine trace drew {drawn['sine']} points, so it is "
            f"being clipped to `sx <= w * 0` again"
        )
        assert drawn["bright"] > 20, (
            f"{label}: only {drawn['bright']} lit bars, so the field is dimmed "
            f"to its unlit 22% again"
        )
    ok("resizing redraws the finished field: 14 findings, the sine and the bars")

    # and the redraw is per frame rather than 120ms after letting go, or the
    # canvas is simply blank for the length of a drag
    assert "requestAnimationFrame(function () { pending = false; resize(); })" in SCRIPT
    assert "setTimeout(resize" not in SCRIPT, (
        "a debounced resize leaves the canvas cleared but not redrawn mid-drag"
    )
    ok("the resize redraw is coalesced per frame, not deferred past the drag")

print("\nall site checks passed.")
