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
    # tools, install, configure, run
    assert len(covering) == 4, f"{platform} has {len(covering)} panes, expected 4"
ok(f"all three platforms are offered, each covered by 4 of the {len(panes)} panes")

# the prerequisites are named, per platform, and the clone is not assumed
assert "winget install Python.Python" in HTML and "winget install Git.Git" in HTML
assert "sudo apt install python3 python3-venv git" in HTML, (
    "Debian needs python3-venv separately; leaving it out is a broken first run"
)
assert "sudo dnf install" in HTML and "sudo pacman -S" in HTML
assert "xcode-select --install" in HTML and "brew install python@" in HTML
assert "git clone https://github.com/mireyacs/rotector-selfbot" in HTML, (
    "the page cannot start in a directory it never told the reader how to get"
)
ok("Python and git installs are given for each platform, and the clone is shown")

# the commands really do differ, or the picker would be theatre
assert "py -3 -m venv" in HTML and r".venv\Scripts\pip" in HTML
assert "python3 -m venv .venv" in HTML and "./.venv/bin/pip" in HTML
assert "copy config.example.toml" in HTML and "cp config.example.toml" in HTML
ok("Windows gets py/Scripts/copy where the others get python3/bin/cp")

# without scripting every platform stays on the page, labelled
assert ".js .os-pane[hidden] { display: none; }" in HTML, (
    "panes must only be hidden once the picker is running"
)
assert "os-pane__name" in HTML, "each pane needs a heading for the no-JS case"
ok("with no JavaScript all three stay visible and labelled")


# --- scrolling, and the bar beside it -------------------------------------
assert "html { scroll-behavior: auto; }" in HTML, (
    "smoothness must not be on by default, or a shared #setup link animates"
)
assert "html.smooth { scroll-behavior: smooth; }" in HTML
assert "html, html.smooth { scroll-behavior: auto !important; }" in HTML, (
    "reduced motion has to switch scrolling off with everything else"
)
ok("smooth scrolling is a class, and reduced motion turns it back off")

assert "scrollbar-color: var(--texture-2) var(--ground)" in HTML
assert "html.on-light { color-scheme: light; scrollbar-color: #c4c4c4" in HTML
assert "::-webkit-scrollbar-thumb { background: var(--texture-2)" in HTML
assert "border-radius: 0" in HTML, "the bar is square like everything else"
ok("the scrollbar is styled for both tones, in both engines, with no radius")


# every literal font-size has to be a step DESIGN.md documents; the ramp is
# eight steps precisely so a ninth is drift rather than a decision
RAMP = {".62rem", ".68rem", ".78rem", ".82rem", "1rem", "1.02rem", "1.2rem", "1.5rem"}
sizes = set(re.findall(r"font-size: ([0-9.]+rem)", HTML))
stray = {size for size in sizes if size not in RAMP}
assert not stray, f"font sizes off the documented ramp: {sorted(stray)}"
ok(f"all {len(sizes)} literal font sizes sit on the documented type ramp")

# --- motion, and the switch that stops it ---------------------------------
# Everything that moves is gated on one class, so a single control can stop
# the lot -- WCAG 2.2.2 asks for exactly that of anything moving on its own.
for selector in (".bars", ".node__hd .chip", ".ledger .meter", ".btn .tick",
                 ".wave path", ".ids"):
    assert f"html.motion {selector}" in HTML, f"{selector} is not gated on .motion"
ok("every moving element hangs off html.motion, so one switch stops them all")

# built by script rather than sitting in the markup, so that a page with no
# JavaScript never shows a control that could not do anything
assert ".motion-toggle {" in HTML, "the control has no styles"
assert "button.className = 'motion-toggle'" in HTML, "the control is never built"
assert "aria-pressed" in HTML and "aria-label" in HTML, "the control is unlabelled"
assert "@media (prefers-reduced-motion: reduce)" in HTML
assert "* { animation: none !important; transition: none !important; }" in HTML
ok("there is a real control, and reduced motion stops the animations outright")

# the recordings are attached to figures, with the still kept as the source
for name in ("results", "sources", "proxies"):
    recording = ROOT / "docs" / "screenshots" / f"{name}.webp"
    assert recording.is_file(), f"{name}.webp is missing; run tools/motion.py"
    assert f'src="screenshots/{name}.svg" data-motion="screenshots/{name}.webp"' in HTML, (
        f"{name}: the still has to stay the src, or no-JS and motion-off lose it"
    )
size = sum((ROOT / "docs" / "screenshots" / f"{n}.webp").stat().st_size
           for n in ("results", "sources", "proxies"))
assert size < 900_000, f"the recordings weigh {size // 1024} KB together"
ok(f"three recordings, {size // 1024} KB, each behind its own still")

# every webp that exists is actually used, or it is dead weight in the repo
for recording in sorted((ROOT / "docs" / "screenshots").glob("*.webp")):
    assert recording.name in HTML, f"{recording.name} is generated but never shown"
ok("no recording ships without the page showing it")


# --- the music player -----------------------------------------------------
for piece in ("player__slot", "player__clip", "player__field", "player__seek",
              "player__time", 'data-act="prev"', 'data-act="next"'):
    assert piece in HTML, f"the player is missing {piece}"
# opened by unrolling: grid-template-rows animates where height: auto cannot,
# so the panel fits whatever the track title needs
assert "grid-template-rows: 0fr" in HTML and ".player.open .player__slot" in HTML
assert "transition: grid-template-rows" in HTML
ok("the player opens by unrolling, and carries prev / seek / next")

# The field is behind the content, not beside it -- but only behind the part it
# actually sits under. It used to span the whole panel, which put a plate under
# the seekbar and its time: opaque enough to read as a box the transport was
# stuck inside. Confining the canvas to the head fixes that at the source, so
# the stacking guard belongs to the head's children and the transport needs no
# lift at all. If the canvas ever escapes back out to the body, the plate is
# back, so assert the containment rather than just the z-index.
assert ".player__head > *:not(.player__field) { position: relative; z-index: 1; }" in HTML
assert HTML.index('class="player__field"') > HTML.index('class="player__head"')
assert HTML.index('class="player__seek') > HTML.index("</div>", HTML.index('class="player__field"')), (
    "the seekbar must sit outside the head, or the field draws under it again"
)
assert "'/peaks/'" in HTML, "the field must read the shipped envelope"
assert "mireyacs/openlofi" in HTML, "one library, not two"
ok("the barcode field sits behind the panel and reads the same envelope as the app")

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
// only the hero-canvas IIFE: the later blocks build a control and observe
// figures, and stubbing all of that would test the harness, not the field
const whole = html.slice(html.lastIndexOf('<script>') + 8, html.lastIndexOf('</script>'));
const script = whole.slice(0, whole.indexOf('/* \u2500\u2500 motion: the switch'));
let motionOn = false;   // the loop is tested separately; here it must settle
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
// the page ships several IIFEs in one block; eval runs them all, so the
// stubs have to satisfy the scroll and picker code too
global.location = { hash: '' };
global.document = { getElementById:id=>(id==='field'?canvas:null),
  querySelector:()=>null, querySelectorAll:()=>[], addEventListener(){},
  // the canvas asks whether motion is running before it loops again
  documentElement:{ className:'',
    classList:{ add(){}, toggle(){}, contains: () => motionOn } } };
let rafFn = null;
global.requestAnimationFrame = fn => { rafFn = fn; return 1; };
global.cancelAnimationFrame = ()=>{};
global.setTimeout = fn => 1;
global.clearTimeout = ()=>{};
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

    # --- the player is silent and lazy until asked ------------------------
    # It must not autoplay -- browsers block it and unrequested sound is the
    # rudest thing a page can do -- and must not fetch a 3 MB track, or even
    # the manifest, for somebody who never opened it.
    harness = 'const PAGE_PATH = "PAGEPATH";\nconst fs = require(\'fs\');\nconst html = fs.readFileSync(PAGE_PATH,\'utf8\');\nconst S = html.slice(html.lastIndexOf(\'<script>\')+8, html.lastIndexOf(\'</script>\'));\nconst block = S.slice(S.indexOf(\'/* ── the music player\'), S.indexOf(\'/* ── scrolling, and the bar\'));\n\nlet played = 0, fetched = [];\nconst listeners = {};\nfunction el(tag) {\n  const node = {\n    tag, className: \'\', innerHTML: \'\', textContent: \'\', style: {},\n    dataset: {}, attrs: {}, children: [], disabled: false,\n    classList: { _s: new Set(),\n      add(c){this._s.add(c);}, remove(c){this._s.delete(c);},\n      toggle(c,on){ on===undefined ? (this._s.has(c)?this._s.delete(c):this._s.add(c)) : (on?this._s.add(c):this._s.delete(c)); return this._s.has(c); },\n      contains(c){return this._s.has(c);} },\n    setAttribute(k,v){this.attrs[k]=v;}, getAttribute(k){return this.attrs[k];},\n    appendChild(c){this.children.push(c); return c;},\n    insertBefore(c){this.children.unshift(c); return c;},\n    addEventListener(n,f){ (this._l=this._l||{})[n]=f; },\n    querySelector(sel){ return el(\'stub\'); },\n    getBoundingClientRect(){ return {left:0,width:100,top:0,height:12}; },\n    getContext(){ return { setTransform(){}, clearRect(){}, fillRect(){}, fillStyle:\'\' }; },\n    get clientWidth(){ return 300; }, get clientHeight(){ return 140; },\n  };\n  return node;\n}\nglobal.window = { devicePixelRatio: 1, addEventListener(){} };\nglobal.document = { querySelector: () => null, createElement: el, body: el(\'body\') };\nglobal.Audio = function () {\n  const a = el(\'audio\');\n  a.paused = true; a.currentTime = 0; a.duration = NaN; a.src = \'\';\n  a.play = () => { played++; return Promise.resolve(); };\n  a.pause = () => {};\n  return a;\n};\nglobal.fetch = (url) => { fetched.push(url); return Promise.resolve({ok:false, status:404}); };\nglobal.requestAnimationFrame = () => 1;\nglobal.cancelAnimationFrame = () => {};\nglobal.atob = s => Buffer.from(s, \'base64\').toString(\'binary\');\nglobal.Uint8Array = Uint8Array;\n\neval(block);\n\n\nconsole.log(JSON.stringify({ played: played, fetched: fetched.length }));\n'.replace("PAGEPATH", str(PAGE))
    lazy = _node(harness)
    assert lazy["played"] == 0, "the player started audio on page load"
    assert lazy["fetched"] == 0, "the player hit the network on page load"
    ok("the player plays nothing and fetches nothing until it is opened")

    # --- the hero field cycles, and the example scan counts with it -------
    harness = 'const PAGE_PATH = "PAGEPATH";\nconst fs = require(\'fs\');\nconst html = fs.readFileSync(PAGE_PATH,\'utf8\');\nconst whole = html.slice(html.lastIndexOf(\'<script>\')+8, html.lastIndexOf(\'</script>\'));\nconst script = whole.slice(0, whole.indexOf(\'/* ── motion: the switch\'));\n\nlet motionOn = true;\nlet ops = { rects: [], pts: 0, alphas: [] };\nconst ctx = { fillStyle:\'\', strokeStyle:\'\', globalAlpha:1, lineWidth:1,\n  setTransform(){}, fillRect(x,y,w,h){ ops.rects.push({x,w,h,a:this.globalAlpha}); ops.alphas.push(this.globalAlpha); },\n  beginPath(){}, moveTo(){ ops.pts++; }, lineTo(){ ops.pts++; }, stroke(){} };\nconst canvas = { getContext:()=>ctx, width:0, height:0,\n                 getBoundingClientRect:()=>({width:1400, height:900}) };\nconst counters = { \'count-members\': {textContent:\'10,833\'}, \'count-findings\': {textContent:\'14\'} };\nconst L = {};\nglobal.window = { devicePixelRatio:1,\n  matchMedia:()=>({matches:false, addEventListener(){}}),\n  addEventListener:(n,f)=>{ (L[n]=L[n]||[]).push(f); },\n  dispatchEvent(e){ (L[e.type]||[]).forEach(f=>f(e)); } };\nglobal.location = { hash:\'\' };\nglobal.document = {\n  getElementById: id => (id===\'field\' ? canvas : counters[id] || null),\n  querySelector:()=>null, querySelectorAll:()=>[], addEventListener(){},\n  documentElement:{ className:\'\', classList:{ add(){}, toggle(){}, contains:()=>motionOn } } };\nlet rafFn = null;\nglobal.requestAnimationFrame = fn => { rafFn = fn; return 1; };\nglobal.cancelAnimationFrame = ()=>{};\nglobal.setTimeout = fn => 1; global.clearTimeout = ()=>{};\neval(script);\n\nfunction tick(ts) { const f = rafFn; rafFn = null; ops = {rects:[],pts:0,alphas:[]}; if (f) f(ts); }\nfunction stats(){ const findings = ops.rects.filter(r=>r.w===9&&r.h===9).length;\n  // ignore the opaque background clear at the top of draw(); the field is\n  // everything narrow enough to be a bar or a mark\n  const field = ops.rects.filter(r => r.w <= 30).map(r => r.a);\n  const maxA = field.length ? Math.max(...field) : 0;\n  return { findings, maxA: +maxA.toFixed(3), members: counters[\'count-members\'].textContent,\n           found: counters[\'count-findings\'].textContent }; }\n\n\nconst out = { settled: stats() };\nL[\'rsb:motion\'].forEach(f => f({type:\'rsb:motion\', detail:true}));\ntick(0);    out.sweepStart = stats();\ntick(1100); out.sweepMid   = stats();\ntick(2200); out.sweepEnd   = stats();\nconst markXs = () => ops.rects.filter(r=>r.w===9&&r.h===9).map(r=>r.x).sort((a,b)=>a-b).join(\',\');\nconst first = markXs();\ntick(4900);                      // hold elapses\ntick(5200); out.fadeEarly = stats();\ntick(5700); out.fadeLate  = stats();\ntick(5800); out.reseed    = stats();          // fade done: rebuild + reset\ntick(8000); out.secondPass = stats();\nout.wallChanged = markXs() !== first;\nops = {rects:[],pts:0,alphas:[]};\nmotionOn = false;\nL[\'rsb:motion\'].forEach(f => f({type:\'rsb:motion\', detail:false}));\nout.stopped = stats();\nconsole.log(JSON.stringify(out));\n'.replace("PAGEPATH", str(PAGE))
    c = _node(harness)

    assert c["settled"]["findings"] == 14 and c["settled"]["members"] == "10,833", c["settled"]
    ok("with motion off the field rests whole, at the figures it reports")

    assert c["sweepStart"]["members"] == "0" and c["sweepStart"]["found"] == "0"
    assert c["sweepMid"]["found"] not in ("0", "14"), c["sweepMid"]
    assert c["sweepEnd"]["members"] == "10,833" and c["sweepEnd"]["found"] == "14"
    ok(f"the example scan counts up with the pass: 0 -> {c['sweepMid']['members']} -> 10,833")

    assert c["fadeEarly"]["maxA"] < 1, "the wall is not fading before the next one"
    assert c["fadeLate"]["maxA"] < c["fadeEarly"]["maxA"], (c["fadeEarly"], c["fadeLate"])
    ok(f"the wall fades out first: {c['fadeEarly']['maxA']} then {c['fadeLate']['maxA']}")

    assert c["reseed"]["members"] == "0" and c["reseed"]["found"] == "0"
    assert c["wallChanged"], "every pass lays out the same wall; positions are fixed"
    ok("each pass lays out a different wall and restarts the count")

    assert c["stopped"]["findings"] == 14 and c["stopped"]["maxA"] == 1
    assert c["stopped"]["members"] == "10,833" and c["stopped"]["found"] == "14"
    ok("stopping the motion leaves one whole wall and the real figures")

    # and the redraw is per frame rather than 120ms after letting go, or the
    # canvas is simply blank for the length of a drag
    assert "requestAnimationFrame(function () { pending = false; resize(); })" in SCRIPT
    assert "setTimeout(resize" not in SCRIPT, (
        "a debounced resize leaves the canvas cleared but not redrawn mid-drag"
    )
    ok("the resize redraw is coalesced per frame, not deferred past the drag")

    # --- a shared link arrives, it does not travel -----------------------
    block = SCRIPT[SCRIPT.index("/* \u2500\u2500 scrolling, and the bar beside it"):
                   SCRIPT.index("/* \u2500\u2500 platform picker")]
    harness = (
        "const block = " + json.dumps(block) + ";\n"
        r"""
function band(invert, top, bottom) {
  return { classList: { contains: c => invert && c === 'invert' },
           getBoundingClientRect: () => ({ top, bottom }) };
}
function run(hash, bands, viewport) {
  const cls = new Set();
  const root = { classList: { add: c => cls.add(c),
    toggle: (c, on) => { on ? cls.add(c) : cls.delete(c); } } };
  const L = {}; let rafs = [];
  global.location = { hash };
  global.window = { innerHeight: viewport,
                    addEventListener: (n,f) => { (L[n] = L[n] || []).push(f); } };
  // the tone code queries twice: once for the bands, once for the chips that
  // sit on top of them. No chips in this harness, so that one is empty.
  global.document = {
    documentElement: root,
    querySelectorAll: sel =>
      sel.indexOf('motion-toggle') >= 0 || sel.indexOf('.player') >= 0 ? [] : bands,
    querySelector: () => null,
  };
  global.requestAnimationFrame = fn => { rafs.push(fn); return 1; };
  eval(block);
  const before = cls.has('smooth');
  (L.load || []).forEach(f => f());
  for (let i = 0; i < 6; i++) { const q = rafs; rafs = []; q.forEach(f => f()); }
  return { before, after: cls.has('smooth'), light: cls.has('on-light') };
}
const V = 900;
console.log(JSON.stringify({
  plain:      run('',       [band(false, 0, 900)], V),
  shared:     run('#setup', [band(false, 0, 900)], V),
  mostlyDark: run('',       [band(false, -100, 700), band(true, 700, 1600)], V),
  mostlyLight:run('',       [band(false, -900, 100), band(true, 100, 1200)], V),
  tallLight:  run('',       [band(true, -2000, 3000)], V),
}));
"""
    )
    r = _node(harness)
    assert r["plain"]["before"] is True, "with no hash, smoothness can be armed at once"
    assert r["shared"]["before"] is False, (
        "a shared #setup link would animate on arrival: smoothness was armed "
        "before the browser finished its jump"
    )
    assert r["shared"]["after"] is True, "smoothness never armed after the jump"
    ok("a shared section link arrives instantly; clicks afterwards are smooth")

    assert r["mostlyDark"]["light"] is False
    assert r["mostlyLight"]["light"] is True
    assert r["tallLight"]["light"] is True, (
        "a band taller than the viewport still has to set the tone"
    )
    ok("the scrollbar follows whichever band covers most of the viewport")


print("\nall site checks passed.")
