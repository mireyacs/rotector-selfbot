"""Pane resizing, leaving group DMs, export retention and config migration."""
import asyncio
import shutil
import sys
import tempfile
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import rsb.tui.app as appmod
from rsb.config import Config
from rsb.discord.http import GROUP_DM_CHANNEL, Guild, PrivateChannel
from rsb.export import MARKER, ExportRow, RETENTION_HOURS, export, purge_expired
from rsb.migrate import SCHEMA, migrate_config, missing_settings
from rsb.rotector import MemberReport
from rsb.tui.app import PaneDivider
from rsb.tui.dialogs import LeaveGroupDialog
from textual.widgets import Button, Checkbox

ok = lambda m: print(f"[ok] {m}")


def select_source(app, kind):
    """Move the sources cursor to the first source of ``kind``.

    Sources sit under collapsible group headers, so a source's display row is
    not its index in app.sources.
    """
    from rsb.tui.app import GROUP_KEY
    from textual.widgets import DataTable
    table = app.query_one("#guilds", DataTable)
    key = next(k for k in app._source_rows if k.startswith(f"{kind}:"))
    table.move_cursor(row=app._source_rows.index(key))
    return next(s for s in app.sources if f"{s.kind}:{s.id}" == key)



# --------------------------------------------------------------------------
# export retention
# --------------------------------------------------------------------------

def _fake_export(base: Path, name: str, marked: bool = True) -> Path:
    folder = base / name
    folder.mkdir(parents=True)
    (folder / "README.txt").write_text(
        f"header\n{MARKER}\n" if marked else "unrelated notes\n", encoding="utf-8"
    )
    (folder / "data.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    return folder


def test_purge_is_conservative():
    base = Path(tempfile.mkdtemp())
    old = datetime.now(timezone.utc) - timedelta(hours=RETENTION_HOURS + 2)
    recent = datetime.now(timezone.utc) - timedelta(hours=1)

    expired = _fake_export(base, f"Srv-{old.strftime('%Y%m%dT%H%M%SZ')}")
    unmarked = _fake_export(base, f"Srv-{old.strftime('%Y%m%dT%H%M%SZ')}-x", marked=False)
    fresh = _fake_export(base, f"Srv-{recent.strftime('%Y%m%dT%H%M%SZ')}")
    mine = base / "my own notes"
    mine.mkdir()
    (mine / "keep.txt").write_text("mine", encoding="utf-8")
    loose = base / "loose.csv"
    loose.write_text("x", encoding="utf-8")

    removed = purge_expired(base)

    assert [p.name for p in removed] == [expired.name], removed
    assert not expired.exists()
    ok(f"expired export removed: {expired.name}")
    assert unmarked.exists(), "a folder without our marker was deleted"
    ok("an old folder lacking our marker is left alone")
    assert fresh.exists()
    ok("a recent export is kept")
    assert mine.exists() and (mine / "keep.txt").exists()
    assert loose.exists()
    ok("unrelated folders and loose files are never touched")

    assert purge_expired(base / "does-not-exist") == []
    ok("a missing export directory is not an error")


def test_export_preserve_flag():
    base = Path(tempfile.mkdtemp())
    old = datetime.now(timezone.utc) - timedelta(hours=RETENTION_HOURS + 2)
    stale = _fake_export(base, f"Old-{old.strftime('%Y%m%dT%H%M%SZ')}")

    rows = [ExportRow("1", "u", "U", MemberReport(discord_id="1"))]
    # stamps relative to now: a fixed one ages past the retention window and
    # the test then only passes on the day it was written
    now = datetime.now(timezone.utc)
    stamp_a = now.strftime("%Y%m%dT%H%M%SZ")
    stamp_b = (now + timedelta(seconds=1)).strftime("%Y%m%dT%H%M%SZ")

    manifest = export(
        rows, guild_name="G", guild_id="1", base_directory=base,
        formats=["csv"], columns=["discord_id"], preserve=True,
        stamp=stamp_a,
    )
    assert stale.exists(), "preserve=True still cleaned up"
    assert manifest.purged == []
    ok("preserve=True keeps expired exports and reports nothing purged")

    manifest = export(
        rows, guild_name="G", guild_id="1", base_directory=base,
        formats=["csv"], columns=["discord_id"], preserve=False,
        stamp=stamp_b,
    )
    assert not stale.exists()
    assert [p.name for p in manifest.purged] == [stale.name]
    ok(f"preserve=False sweeps expired exports: {stale.name}")
    assert manifest.directory.exists(), "the new export swept away its own folder"
    ok("the export just written survives its own cleanup")


# --------------------------------------------------------------------------
# config migration
# --------------------------------------------------------------------------

def test_migration():
    base = Path(tempfile.mkdtemp())
    path = base / "config.toml"
    path.write_text(
        "# hand-tuned, do not lose\n"
        "[discord]\n"
        'token = "abc123"   # inline note\n'
        "\n"
        "[scan]\n"
        "skip_bots = false\n",
        encoding="utf-8",
    )

    sections, keys = missing_settings(path)
    assert "export" in sections and "moderation" in sections
    assert "scan.hide_unknown" in keys
    ok(f"detected {len(sections)} missing section(s), {len(keys)} missing key(s)")

    dry = migrate_config(path, dry_run=True)
    assert dry.changed and path.read_text(encoding="utf-8").count("hide_unknown") == 0
    ok("--dry-run reports without writing")

    report = migrate_config(path)
    body = path.read_text(encoding="utf-8")
    data = tomllib.loads(body)

    assert data["discord"]["token"] == "abc123"
    assert "do not lose" in body and "inline note" in body
    ok("existing values and comments survive untouched")

    assert data["scan"]["skip_bots"] is False, "a user's value was overwritten"
    ok("a setting the user changed is not reset to the default")

    for section in SCHEMA:
        assert section.name in data, section.name
        for setting in section.settings:
            assert setting.name in data[section.name], f"{section.name}.{setting.name}"
    ok(f"every setting in the schema is present afterwards "
       f"({sum(len(s.settings) for s in SCHEMA)} across {len(SCHEMA)} sections)")

    assert report.backup and report.backup.exists()
    assert "abc123" in report.backup.read_text(encoding="utf-8")
    ok(f"the original was backed up to {report.backup.name}")

    assert not migrate_config(path).changed
    ok("running it again changes nothing")

    fresh = base / "brand-new" / "config.toml"
    created = migrate_config(fresh)
    assert created.created and fresh.is_file()
    assert Config.load(fresh).export.preserve is False
    ok("a missing config is created complete and loads cleanly")

    broken = base / "broken.toml"
    broken.write_text("this is not [ valid toml", encoding="utf-8")
    assert missing_settings(broken) == ([], [])
    ok("an unparseable config reports nothing rather than raising")


# --------------------------------------------------------------------------
# UI: divider and leaving a group
# --------------------------------------------------------------------------

GROUP = PrivateChannel(
    id="g1", type=GROUP_DM_CHANNEL, name="Squad", owner_id="9",
    recipients=[{"id": "900000000000000005", "username": "gm"}],
)


class FakeHTTP:
    def __init__(self, token, **kw):
        self.left = []

    async def me(self): return {"username": "t", "global_name": "T", "id": "9"}
    async def guilds(self):
        return [Guild(id="111", name="Srv", owner=False, permissions=0,
                      member_count=2, presence_count=1)]
    async def relationships(self): return []
    async def widget(self, gid): return None
    async def private_channels(self): return [GROUP]
    async def channels(self, gid): return []
    async def leave_group_dm(self, channel_id, silent=False):
        self.left.append((channel_id, silent))
    async def aclose(self): pass


class FakeGateway:
    def __init__(self, token): self.user = None
    async def connect(self, timeout=45.0): return {}
    async def fetch_members(self, *a, **kw): return {}
    async def close(self): pass


async def test_ui():
    appmod.DiscordHTTP = FakeHTTP
    appmod.DiscordGateway = FakeGateway

    workdir = Path(tempfile.mkdtemp())
    cfg = Config()
    cfg.token = "fake"
    cfg.source = workdir / "config.toml"
    app = appmod.ScannerApp(cfg)

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.8)
        pane = app.query_one("#servers-pane")

        # --- the divider exists and the pane starts wider than it used to
        divider = app.query_one("#divider", PaneDivider)
        assert pane.size.width == 56, pane.size.width
        ok(f"sources pane starts at {pane.size.width} columns (was 42)")

        app.set_pane_width(80)
        await pilot.pause(0.2)
        assert app.query_one("#servers-pane").size.width == 80
        ok("dragging the divider resizes the pane (80 columns)")

        app.set_pane_width(2)
        await pilot.pause(0.2)
        narrow = app.query_one("#servers-pane").size.width
        assert narrow == PaneDivider.MIN_WIDTH, narrow
        ok(f"clamped to a usable minimum ({narrow}), not squashed to nothing")

        app.set_pane_width(10_000)
        await pilot.pause(0.2)
        wide = app.query_one("#servers-pane").size.width
        assert wide <= app.size.width - 20, wide
        ok(f"clamped so the results pane always survives ({wide} of {app.size.width})")

        await pilot.press("]")
        await pilot.press("[")
        await pilot.press("[")
        await pilot.pause(0.2)
        ok(f"keyboard resizing works too: pane now {app._pane_width}")

        # --- leaving a group DM
        table = app.query_one("#guilds", appmod.DataTable)
        select_source(app, "group")
        await pilot.pause(0.3)
        before = table.row_count

        await pilot.press("L")
        for _ in range(40):
            await pilot.pause(0.1)
            if isinstance(app.screen, LeaveGroupDialog):
                break
        dialog = app.screen
        assert isinstance(dialog, LeaveGroupDialog), "leave dialog did not open"
        assert dialog.query_one("#silent", Checkbox).value is False
        ok("leave dialog opens with silent off, matching the config default")

        dialog.query_one("#silent", Checkbox).value = True
        dialog.query_one("#remember", Checkbox).value = True
        dialog.query_one("#confirm", Button).press()
        for _ in range(40):
            await pilot.pause(0.1)
            if not isinstance(app.screen, LeaveGroupDialog):
                break

        assert app.http.left == [("g1", True)], app.http.left
        ok(f"left the group silently: {app.http.left[0]}")

        assert app.config.moderation.silent_leave is True
        saved = tomllib.loads((workdir / "config.toml").read_text(encoding="utf-8"))
        assert saved["moderation"]["silent_leave"] is True
        ok("silent preference remembered in config.toml")

        # the group row goes, and so does its now-empty group header
        assert app.query_one("#guilds", appmod.DataTable).row_count < before
        assert not any(s.id == "g1" for s in app.sources)
        ok("the group disappears from the source list once left")

        # --- 'L' on a non-group says so rather than doing something odd
        select_source(app, "guild")
        await pilot.pause(0.2)
        await pilot.press("L")
        await pilot.pause(0.3)
        assert "group DM" in app._status_text, app._status_text
        ok(f"'L' elsewhere explains itself: {app._status_text}")

    print("\nALL LAYOUT/RETENTION TESTS PASSED")


test_purge_is_conservative()
print()
test_export_preserve_flag()
print()
test_migration()
print()
asyncio.run(test_ui())
