"""Hot reload, error recovery, scan merge modes, and export scope.

The reload tests edit a real module on disk and check the change takes effect
in the running process, because a reloader that reports success without
actually swapping the code is worse than none.
"""
import asyncio
import sys
import tempfile
import time
from pathlib import Path
from types import ModuleType

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import rsb.tui.app as appmod
import rsb.verdict as verdict_module
from rsb.config import Config
from rsb.discord.gateway import GuildMember
from rsb.discord.http import Channel, Guild
from rsb.hotreload import PINNED, HotReloader, rebind
from rsb.rotector import MemberReport, RobloxAccount
from rsb.tui.app import Row
from rsb.tui.dialogs import ExportDialog, RescanDialog
from rsb.tui.settings import ErrorScreen
from rsb.verdict import Verdict
from textual.widgets import Button, Checkbox, DataTable, Input, RadioButton

ok = lambda m: print(f"[ok] {m}")

ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------
# hot reload
#
# Deliberately against a throwaway package rather than the real source: an
# earlier version of this test edited rsb/verdict.py, and when it failed
# part-way it left the tree corrupted. A test that can damage the code it is
# testing is not worth the coverage.
# --------------------------------------------------------------------------

def make_probe(tmp: Path, value: int) -> Path:
    package = tmp / "reloadprobe"
    package.mkdir(exist_ok=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    module = package / "thing.py"
    module.write_text(
        f"CONSTANT = {value}\n\n\ndef value():\n    return {value}\n",
        encoding="utf-8",
    )
    return module


def touch(path: Path) -> None:
    """Nudge the mtime; deliberately by less than a second."""
    import os

    stamp = max(time.time(), path.stat().st_mtime) + 0.01
    os.utime(path, (stamp, stamp))


def test_reload_picks_up_a_real_edit():
    tmp = Path(tempfile.mkdtemp())
    module = make_probe(tmp, 1)
    sys.path.insert(0, str(tmp))
    try:
        import reloadprobe.thing as probe

        reloader = HotReloader(pinned=set(), prefixes=("reloadprobe",))
        assert reloader.changed() == []
        assert not reloader.reload().changed
        ok("a clean tree reports nothing to reload, and reloading is a no-op")

        assert probe.value() == 1
        module.write_text(
            "CONSTANT = 2\n\n\ndef value():\n    return 2\n", encoding="utf-8"
        )
        touch(module)

        changed = reloader.changed()
        assert changed == ["reloadprobe.thing"], changed
        report = reloader.reload()
        assert report.reloaded == ["reloadprobe.thing"] and not report.failed
        ok(f"an edit on disk is detected and applied: {report.describe()}")

        assert probe.value() == 2 and probe.CONSTANT == 2
        ok("the running process uses the edited code, constants included")

        # rapid edits of identical length inside one second are the case the
        # bytecode cache gets wrong
        for want in (3, 1, 4, 1):
            module.write_text(
                f"CONSTANT = {want}\n\n\ndef value():\n    return {want}\n",
                encoding="utf-8",
            )
            touch(module)
            reloader.reload()
            assert probe.value() == want, (want, probe.value())
        ok("four same-length edits inside a second each took effect, "
           "not a stale .pyc")
    finally:
        sys.path.remove(str(tmp))
        sys.modules.pop("reloadprobe.thing", None)
        sys.modules.pop("reloadprobe", None)


def test_rebind_repoints_direct_imports():
    tmp = Path(tempfile.mkdtemp())
    module = make_probe(tmp, 1)
    sys.path.insert(0, str(tmp))
    try:
        import reloadprobe.thing as probe

        # an importer holding a direct reference, as every module here does
        holder = ModuleType("holder")
        holder.value = probe.value
        sys.modules["holder"] = holder
        assert holder.value() == 1

        reloader = HotReloader(pinned=set(), prefixes=("reloadprobe",))
        module.write_text(
            "CONSTANT = 9\n\n\ndef value():\n    return 9\n", encoding="utf-8"
        )
        touch(module)
        report = reloader.reload()

        assert probe.value() == 9
        assert holder.value() == 9, (
            "a `from x import y` reference still points at the old function"
        )
        ok(f"{report.rebound} direct import(s) repointed, so importers see the "
           f"change too")
    finally:
        sys.modules.pop("holder", None)
        sys.path.remove(str(tmp))
        sys.modules.pop("reloadprobe.thing", None)
        sys.modules.pop("reloadprobe", None)


def test_ui_modules_are_pinned():
    reloader = HotReloader()
    for name in ("rsb.tui.app", "rsb.tui.dialogs", "rsb.hotreload"):
        assert name in PINNED, name
    watched = {n for n, _m in reloader._watched()}
    assert not (watched & PINNED), watched & PINNED
    ok(f"the {len(PINNED)} UI/self modules are never reloaded under a live app")

    report = reloader.reload(["rsb.tui.app"])
    assert "rsb.tui.app" in report.skipped and not report.reloaded
    ok("asking for one anyway is refused rather than crashing the app")


def test_broken_edit_is_reported_not_raised():
    tmp = Path(tempfile.mkdtemp())
    module = make_probe(tmp, 1)
    sys.path.insert(0, str(tmp))
    try:
        import reloadprobe.thing as probe

        reloader = HotReloader(pinned=set(), prefixes=("reloadprobe",))
        module.write_text("def value(:\n  broken\n", encoding="utf-8")
        touch(module)

        report = reloader.reload()
        assert report.failed, report
        assert report.failed[0][0] == "reloadprobe.thing"
        assert "Error" in report.failed[0][1]
        ok(f"a syntax error is reported, not raised: {report.describe()[:72]}")

        assert probe.value() == 1
        ok("and the previously working code stays in place")
    finally:
        sys.path.remove(str(tmp))
        sys.modules.pop("reloadprobe.thing", None)
        sys.modules.pop("reloadprobe", None)


def test_real_source_is_never_touched():
    """The reloader must not be able to damage the tree it reloads."""
    before = (ROOT / "rsb" / "verdict.py").read_text(encoding="utf-8")
    HotReloader().reload()
    after = (ROOT / "rsb" / "verdict.py").read_text(encoding="utf-8")
    assert before == after
    from rsb.verdict import Verdict as _V, verdict_label as _label
    assert _label(_V.THREAT) == "THREAT"
    ok("reloading the real package leaves its source, and its behaviour, intact")


# --------------------------------------------------------------------------
# UI: recovery, merge modes, export scope
# --------------------------------------------------------------------------

MEMBERS = {
    "1": GuildMember(id="1", username="flagged"),
    **{
        str(900_000_000_000_000_000 + n): GuildMember(
            id=str(900_000_000_000_000_000 + n), username=f"u{n}"
        )
        for n in range(5)
    },
}


class FakeHTTP:
    def __init__(self, token, **kw): pass
    async def me(self): return {"username": "t", "global_name": "T", "id": "9"}
    async def guilds(self):
        return [Guild(id="111", name="Srv", owner=False, permissions=0,
                      member_count=len(MEMBERS), presence_count=3)]
    async def relationships(self): return []
    async def widget(self, gid): return None
    async def private_channels(self): return []
    async def channels(self, gid):
        return [Channel(id="c1", name="general", type=0, position=0,
                        everyone_can_view=True)]
    async def aclose(self): pass


class FakeGateway:
    def __init__(self, token):
        self.user = None
        self.calls = 0
    async def connect(self, timeout=45.0): return {}
    async def fetch_members(self, gid, channels, expected=None, on_progress=None,
                            on_members=None, **kwargs):
        self.calls += 1
        if on_members:
            on_members(list(MEMBERS.values()))
        return dict(MEMBERS)
    async def close(self): pass


def select_source(app, kind):
    from rsb.tui.app import GROUP_KEY
    table = app.query_one("#guilds", DataTable)
    key = next(k for k in app._source_rows if k.startswith(f"{kind}:"))
    table.move_cursor(row=app._source_rows.index(key))


async def scan(app, pilot, wait_rows=1):
    await pilot.press("s")
    for _ in range(80):
        await pilot.pause(0.15)
        if len(app.rows) >= wait_rows and not app._activity:
            return
    raise AssertionError("scan did not finish")


async def test_merge_modes():
    appmod.DiscordHTTP = FakeHTTP
    appmod.DiscordGateway = FakeGateway
    cfg = Config()
    cfg.token = "fake.test.token"
    cfg.scan.on_rescan = "replace"
    app = appmod.ScannerApp(cfg)

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.9)
        select_source(app, "guild")
        await pilot.pause(0.3)
        await scan(app, pilot, len(MEMBERS))
        first = len(app.rows)
        assert first == len(MEMBERS)
        ok(f"first scan holds {first} members")

        # replace: the table is rebuilt from scratch
        await scan(app, pilot, len(MEMBERS))
        assert len(app.rows) == first
        assert "added to" not in app._status_text
        ok("'replace' rebuilds rather than accumulating")

        # merge_skip: known members are not looked up again
        app.config.scan.on_rescan = "merge_skip"
        await scan(app, pilot, len(MEMBERS))
        assert len(app.rows) == first, len(app.rows)
        assert "already checked" in app._status_text, app._status_text
        assert "may be hidden" not in app._status_text, (
            "an all-known rescan must not look like a failed member read"
        )
        ok(f"'merge_skip' with nothing new says so plainly: "
           f"{app._status_text[:78]}...")

        # a member the earlier pass never saw is picked up
        newcomer = GuildMember(id="900000000000000099", username="latecomer")
        MEMBERS[newcomer.id] = newcomer
        try:
            await scan(app, pilot, first + 1)
            assert len(app.rows) == first + 1, len(app.rows)
            assert newcomer.id in app.rows
            ok("a member only visible on a later pass is added to the existing set")
        finally:
            MEMBERS.pop(newcomer.id, None)

        # merge_recheck: everyone is looked up again
        app.config.scan.on_rescan = "merge_recheck"
        await scan(app, pilot, first)
        assert "added to" in app._status_text, app._status_text
        ok("'merge_recheck' re-checks everyone, since flags change over time")

        # the ask path
        app.config.scan.on_rescan = "ask"
        await pilot.press("s")
        for _ in range(40):
            await pilot.pause(0.1)
            if isinstance(app.screen, RescanDialog):
                break
        assert isinstance(app.screen, RescanDialog), type(app.screen)
        ok("with on_rescan='ask' the choice is offered")
        dialog = app.screen
        dialog.query_one("#mode-merge-skip", RadioButton).value = True
        dialog.query_one("#confirm", Button).press()
        for _ in range(60):
            await pilot.pause(0.15)
            if not isinstance(app.screen, RescanDialog) and not app._activity:
                break
        assert "already checked" in app._status_text, app._status_text
        ok("and honoured")


async def test_export_covers_every_page():
    appmod.DiscordHTTP = FakeHTTP
    appmod.DiscordGateway = FakeGateway
    cfg = Config()
    cfg.token = "fake.test.token"
    app = appmod.ScannerApp(cfg)

    async with app.run_test(size=(150, 40)) as pilot:
        await pilot.pause(0.9)
        workdir = Path(tempfile.mkdtemp())
        app.config.export.directory = str(workdir / "exports")
        app.config.source = workdir / "config.toml"

        total = appmod.PAGE_SIZE * 2 + 30
        for i in range(total):
            member = GuildMember(id=str(i), username=f"user{i:05d}")
            report = MemberReport(discord_id=member.id)
            report.accounts.append(
                RobloxAccount(user_id=i, username=f"r{i}", flag_type=2)
            )
            app.rows[member.id] = Row(member=member, report=report)
        app.current_source = app.sources[-1]
        app.filter_mode = appmod.FilterMode.ALL
        app._page = 1
        app._rebuild_table()
        await pilot.pause(0.3)

        on_page = len(app._shown)
        assert on_page == appmod.PAGE_SIZE and app.page_count == 3
        ok(f"{total} rows across {app.page_count} pages, viewing page 2 "
           f"({on_page} rows)")

        for scope, expected, label in [
            ("filtered", total, "current filter, all pages"),
            ("page", on_page, "this page only"),
            ("all", total, "everything scanned"),
        ]:
            rows = app._export_rows(scope)
            assert len(rows) == expected, (scope, len(rows), expected)
            ok(f"scope {scope!r} -> {len(rows):,} rows ({label})")

        # and through the dialog, from page 2
        app.run_export()
        for _ in range(40):
            await pilot.pause(0.1)
            if isinstance(app.screen, ExportDialog):
                break
        dialog = app.screen
        dialog.query_one("#scope-filtered", RadioButton).value = True
        dialog.query_one("#confirm", Button).press()
        for _ in range(60):
            await pilot.pause(0.1)
            if not isinstance(app.screen, ExportDialog):
                break

        import csv as _csv
        folder = sorted((workdir / "exports").glob("*"))[0]
        parts = sorted(folder.glob("*.csv"))
        written = 0
        for part in parts:
            with part.open(newline="", encoding="utf-8") as handle:
                written += len(list(_csv.reader(handle))) - 1
        assert written == total, (written, total)
        ok(f"exporting from page 2 wrote all {written:,} rows, not the "
           f"{on_page} on screen")


async def test_error_recovery():
    appmod.DiscordHTTP = FakeHTTP
    appmod.DiscordGateway = FakeGateway
    cfg = Config()
    cfg.token = "fake.test.token"
    app = appmod.ScannerApp(cfg)

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.9)
        app.rows["1"] = Row(
            member=GuildMember(id="1", username="kept"),
            report=MemberReport(discord_id="1"),
        )

        retried = []
        app.report_error(
            RuntimeError("the thing broke"),
            context="while doing the thing",
            retry=lambda: retried.append(1),
        )
        for _ in range(40):
            await pilot.pause(0.1)
            if isinstance(app.screen, ErrorScreen):
                break
        assert isinstance(app.screen, ErrorScreen), type(app.screen)
        screen = app.screen
        assert "RuntimeError: the thing broke" in screen.summary
        assert "while doing the thing" in screen.context
        assert "1 scanned members are still loaded" in screen.preserved
        ok(f"the error screen states the failure, the context, and that "
           f"{len(app.rows)} result(s) survived")

        screen.query_one("#retry", Button).press()
        for _ in range(40):
            await pilot.pause(0.1)
            if not isinstance(app.screen, ErrorScreen):
                break
        assert retried == [1], retried
        ok("'Try again' re-runs the operation that failed")
        assert app.rows, "results were lost by the recovery flow"
        ok("and the results are still there afterwards")

        # reload from the error screen
        app.report_error(ValueError("again"), context="second time")
        for _ in range(40):
            await pilot.pause(0.1)
            if isinstance(app.screen, ErrorScreen):
                break
        app.screen.query_one("#reload", Button).press()
        for _ in range(60):
            await pilot.pause(0.1)
            if not isinstance(app.screen, ErrorScreen):
                break
        await pilot.pause(0.6)
        assert app.rows, "reloading discarded the results"
        ok(f"'Reload code' keeps the {len(app.rows)} scanned member(s): "
           f"{app._status_text[:60]}")


async def main():
    test_reload_picks_up_a_real_edit()
    test_rebind_repoints_direct_imports()
    test_ui_modules_are_pinned()
    test_broken_edit_is_reported_not_raised()
    test_real_source_is_never_touched()
    print()
    await test_merge_modes()
    print()
    await test_export_covers_every_page()
    print()
    await test_error_recovery()
    print("\nALL RELOAD/RECOVERY TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
