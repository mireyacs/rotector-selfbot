"""The picked theme is restored next launch, and only when it was really picked.

Driving the real app, because the interesting parts are all in the wiring: the
theme picker applies each theme as you arrow past it, so a naive "write on
change" would save whatever you happened to scroll over, and a headless tool
building a bare ``Config()`` would rewrite the operator's own config.toml --
``config_path()`` falls back to ``./config.toml`` when one exists.
"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rsb.config import Config
from rsb.tui.proxies import ProxyTesterApp
from rsb.tui.theme import NAME as THEME_NAME, ThemeMemory

ok = lambda m: print(f"[ok] {m}")


def _config(tmp: Path, theme: str = "") -> Config:
    path = tmp / "config.toml"
    path.write_text(
        f'[discord]\ntoken = "x"\n\n[ui]\ntheme = "{theme}"\n', encoding="utf-8"
    )
    config = Config.load(path)
    config.proxy.file = "/nonexistent"
    return config


async def main() -> None:
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)

        # -- the theme is registered and offered ---------------------------
        config = _config(tmp)
        app = ProxyTesterApp(config)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.3)
            assert THEME_NAME in app.available_themes, "theme not offered"
            assert app.theme != THEME_NAME, "must not be the default"
            ok(f"{THEME_NAME!r} is in the theme selector and is not forced on")

            # -- browsing does not save ------------------------------------
            # what the picker does while you arrow through the list
            app.theme = "gruvbox"
            await pilot.pause(0.2)
            app.theme = "nord"
            await pilot.pause(0.2)
            saved = Config.load(tmp / "config.toml").ui.theme
            assert saved == "", f"a browse was written to disk as {saved!r}"
            ok("themes tried and abandoned inside the debounce are not saved")

            # -- a choice that settles does ---------------------------------
            app.theme = THEME_NAME
            await pilot.pause(ThemeMemory.DELAY + 1.0)
            saved = Config.load(tmp / "config.toml").ui.theme
            assert saved == THEME_NAME, f"theme not saved (got {saved!r})"
            ok(f"a choice held for {ThemeMemory.DELAY:.0f}s is written to [ui] theme")

            reloaded = Config.load(tmp / "config.toml")
            assert reloaded.token == "x", "saving a theme disturbed [discord]"
            ok("the rest of config.toml survived the write")

        # -- and it comes back next launch ---------------------------------
        app = ProxyTesterApp(_config(tmp, THEME_NAME))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.4)
            assert app.theme == THEME_NAME, app.theme
            ok("the saved theme is restored on the next launch")

        # -- an unknown theme name does not silently do nothing -------------
        app = ProxyTesterApp(_config(tmp, "no-such-theme"))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.4)
            assert app.theme != "no-such-theme"
            ok("an unknown saved theme falls back instead of raising")

        # -- persist_theme=False writes nothing -----------------------------
        # the guard tools/screenshots.py relies on: it drives the real app
        # against a bare Config(), whose config_path() is a real file on disk
        app = ProxyTesterApp(_config(tmp), persist_theme=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.3)
            app.theme = "dracula"
            await pilot.pause(ThemeMemory.DELAY + 1.0)
            assert app.theme == "dracula", "the theme should still apply"
            saved = Config.load(tmp / "config.toml").ui.theme
            assert saved == "", f"persist_theme=False still wrote {saved!r}"
            ok("persist_theme=False applies the theme but writes nothing")


asyncio.run(main())
print("\nall theme persistence checks passed.")
