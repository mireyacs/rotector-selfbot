"""Things that work on Linux and quietly break on Windows or macOS.

Most portability bugs are not exotic; they are a handful of habits that happen
to be harmless on the machine the code was written on. This asserts the ones
this project can be checked for statically, because the alternative is finding
out from somebody else's traceback.

What it cannot do is prove the app runs on Windows -- that needs Windows. What
it can do is stop the specific mistakes that made it not run.
"""
import ast
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ok = lambda m: print(f"[ok] {m}")

SOURCES = [
    p for p in sorted(ROOT.rglob("*.py"))
    if ".venv" not in p.parts and "__pycache__" not in p.parts
]
assert len(SOURCES) > 20, f"only found {len(SOURCES)} source files"


def _tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"))


# --- text IO must name its encoding ---------------------------------------
# Python uses the *locale* encoding when none is given. That is UTF-8 on Linux
# and macOS and cp1252 on most Windows installs, so an export naming a guild
# with an emoji in it raises UnicodeEncodeError there and nowhere else.
unencoded = []
for path in SOURCES:
    for node in ast.walk(_tree(path)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name not in ("read_text", "write_text"):
            continue
        if "encoding" not in {kw.arg for kw in node.keywords}:
            unencoded.append(f"{path.relative_to(ROOT)}:{node.lineno} {name}()")
assert not unencoded, "text IO without an explicit encoding:\n  " + "\n  ".join(unencoded)
ok(f"all {len(SOURCES)} source files name an encoding on every text read/write")

# `open()` in text mode has the same problem
raw_open = []
for path in SOURCES:
    for node in ast.walk(_tree(path)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name != "open":
            continue
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) \
                and func.value.id in ("Image", "io", "gzip", "zipfile"):
            continue  # not a filesystem text open
        modes = [a.value for a in node.args if isinstance(a, ast.Constant)]
        if any(isinstance(m, str) and "b" in m for m in modes):
            continue
        if "encoding" not in {kw.arg for kw in node.keywords}:
            raw_open.append(f"{path.relative_to(ROOT)}:{node.lineno}")
assert not raw_open, "text-mode open() without an encoding:\n  " + "\n  ".join(raw_open)
ok("no text-mode open() relies on the locale encoding either")


# --- no Unix-only imports -------------------------------------------------
UNIX_ONLY = {"fcntl", "termios", "pwd", "grp", "posix", "resource", "tty", "pty",
             "crypt", "syslog", "spwd"}
found = []
for path in SOURCES:
    for node in ast.walk(_tree(path)):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module.split(".")[0]]
        for name in names:
            if name in UNIX_ONLY:
                found.append(f"{path.relative_to(ROOT)}:{node.lineno} {name}")
assert not found, "Unix-only modules imported:\n  " + "\n  ".join(found)
ok("no Unix-only standard library modules are imported")


# --- paths are built, not spelled -----------------------------------------
# A literal "a/b" happens to work on Windows, but a literal "\\" does not work
# anywhere else, and os.sep assumptions in string handling are the usual cause.
from rsb.config import candidate_paths, _config_dir, _xdg_dir, _native_dir  # noqa: E402

paths = candidate_paths()
assert len(paths) == len(set(paths)), f"duplicate config candidates: {paths}"
assert all(isinstance(p, Path) for p in paths)
assert paths[0] == Path.cwd() / "config.toml", "the local file must win"
assert _config_dir() in {p.parent for p in paths}, "the write target must be searched"
assert (_native_dir() is None) == (os.name != "nt")
ok(f"config is searched in {len(paths)} places, all built with pathlib")

# the historical XDG location stays searched wherever the platform default is,
# so an existing install is never silently orphaned
assert _xdg_dir() / "config.toml" in paths
ok("the XDG path stays in the search list on every platform")


# --- the export filename sanitiser survives Windows -----------------------
# Guild names come from strangers and end up in paths. Windows forbids
# <>:"/\|?* in a filename and treats a trailing dot or space as a mistake.
from rsb.export import export  # noqa: E402
import inspect  # noqa: E402

source = inspect.getsource(export)
assert 'c.isalnum() or c in "-_"' in source, (
    "the export stem sanitiser changed; re-check it against Windows' "
    "forbidden characters"
)
sanitise = lambda name: "".join(c if c.isalnum() or c in "-_" else "-" for c in name)[:40]
FORBIDDEN = set('<>:"/\\|?*') | {chr(i) for i in range(32)}
for hostile in ['a<b>c:d"e/f\\g|h?i*j', "trailing dot.", "trailing space ",
                "CON", "NUL", "  ", "..", "🎮 Trading Hub — 東京", "a\x00b"]:
    stem = f"{sanitise(hostile)}-20260803T120000Z"
    assert not (FORBIDDEN & set(stem)), f"{hostile!r} -> {stem!r}"
    assert not stem.endswith((".", " ")), stem
    # a reserved device name only bites as the whole stem; the stamp prevents it
    assert stem.split("-")[0].upper() not in ("CON", "PRN", "AUX", "NUL") or len(
        stem.split("-")
    ) > 1
ok("guild and member names are reduced to characters every filesystem accepts")


# --- the interpreter floor is stated --------------------------------------
from rsb.__main__ import MIN_PYTHON  # noqa: E402

assert MIN_PYTHON >= (3, 11), MIN_PYTHON  # tomllib landed in 3.11
assert sys.version_info >= MIN_PYTHON
guard = (ROOT / "rsb/__main__.py").read_text(encoding="utf-8")
assert "sys.version_info < MIN_PYTHON" in guard, (
    "the version guard must run before the first import that would fail"
)
assert guard.index("MIN_PYTHON") < guard.index("from .config import"), (
    "the guard has to come before importing anything that needs the new version"
)
ok(f"the Python {'.'.join(map(str, MIN_PYTHON))} floor is checked before it can bite")


# --- git is found, not assumed --------------------------------------------
from rsb.update import _env, git_available, preflight  # noqa: E402

env = _env()
assert env.get("GIT_TERMINAL_PROMPT") == "0", "a headless fetch must not prompt"
assert len(env) > 3, "the environment is inherited, not replaced"
for inherited in os.environ:
    assert inherited in env, f"{inherited} was dropped from git's environment"
ok("git inherits the real environment, so SYSTEMROOT and friends survive")

assert isinstance(git_available(), bool)
assert isinstance(preflight(Path(__file__).parent), str)
ok("a missing git is a reported reason, not an exception")


# --- fonts exist for every platform ---------------------------------------
from rsb.imagerender import BOLD_CANDIDATES, FONT_CANDIDATES, _load_font  # noqa: E402

for label, candidates in (("regular", FONT_CANDIDATES), ("bold", BOLD_CANDIDATES)):
    joined = " ".join(candidates).lower()
    assert "windows/fonts" in joined, f"no Windows {label} font candidate"
    assert "/system/library/fonts" in joined, f"no macOS {label} font candidate"
    assert "/usr/share/fonts" in joined, f"no Linux {label} font candidate"
ok("both font lists name a candidate on Linux, macOS and Windows")

# and the fallback has to be sized, or the table's column maths comes apart
regular, bold = _load_font(15), _load_font(15, bold=True)
assert regular.getlength("0") == regular.getlength("W"), "table font is not monospaced"
assert abs(regular.getlength("0") - bold.getlength("0")) < 0.01, (
    "bold and regular must share an advance or the columns will not line up"
)
ok(f"the loaded pair is monospaced and matched ({regular.getlength('0')}px advance)")

print("\nall portability checks passed.")
