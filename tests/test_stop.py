"""Stopping a scan must actually stop everything that animates.

The lookup pipeline runs in a plain asyncio task rather than a Textual worker,
so cancelling the worker group alone left it running -- and a running pipeline
keeps reporting progress, which restarts the spinner and resets the per-task
clock the user just stopped.
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import rsb.tui.app as appmod
from rsb.config import Config
from rsb.discord.gateway import GuildMember
from rsb.discord.http import Channel, Guild

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


WAVES = 8
PER_WAVE = 100
GAP = 0.4


class SlowGateway:
    """Keeps feeding members for a while, so there is something to stop."""

    def __init__(self, token, bot=False):
        self.user = None
        self.waves_sent = 0
        self.finished = False

    async def connect(self, timeout=45.0):
        return {}

    async def fetch_members(self, gid, channels, expected=None, on_progress=None,
                            on_members=None, **kwargs):
        all_members = {}
        for index in range(WAVES):
            batch = [
                GuildMember(id=str(900_000_000_000_000_000 + index * PER_WAVE + n),
                            username=f"u{index}_{n}")
                for n in range(PER_WAVE)
            ]
            for m in batch:
                all_members[m.id] = m
            if on_progress:
                on_progress(len(all_members), expected, "Reading #general member list")
            if on_members:
                on_members(batch)
            self.waves_sent += 1
            await asyncio.sleep(GAP)
        self.finished = True
        return all_members

    async def close(self):
        pass


class FakeHTTP:
    def __init__(self, token, **kw): pass
    async def me(self): return {"username": "t", "global_name": "T", "id": "9"}
    async def guilds(self):
        return [Guild(id="111", name="Big", owner=False, permissions=0,
                      member_count=WAVES * PER_WAVE, presence_count=10)]
    async def relationships(self): return []
    async def widget(self, gid): return None
    async def private_channels(self): return []
    async def channels(self, gid):
        return [Channel(id="c1", name="general", type=0, position=0,
                        everyone_can_view=True)]
    async def aclose(self): pass


async def main():
    appmod.DiscordHTTP = FakeHTTP
    appmod.DiscordGateway = SlowGateway

    cfg = Config()
    cfg.token = "fake"
    cfg.scan.on_rescan = "replace"
    app = appmod.ScannerApp(cfg)

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.8)
        table = app.query_one("#guilds", appmod.DataTable)
        select_source(app, "guild")
        await pilot.pause(0.3)

        await pilot.press("s")
        for _ in range(60):
            await pilot.pause(0.1)
            if app.rows:
                break
        assert app._activity is not None, "scan never started"
        assert app._process_started is not None
        ok(f"scan running: {app._activity[:60]!r}")

        rows_at_stop = len(app.rows)
        gateway = app.gateway

        await pilot.press("x")
        await pilot.pause(0.4)

        assert app._activity is None, f"spinner still active: {app._activity!r}"
        ok("activity cleared the moment the scan was stopped")

        assert app._process_started is None, "total-elapsed clock still running"
        ok("whole-run clock stopped")

        assert app._eta_progress is None
        ok("ETA cleared")

        status = app._compose_status().plain
        assert "task " not in status, status
        assert "ETA" not in status, status
        assert "total" not in status, status
        ok(f"status bar shows no live timers: {status[:70]!r}")

        assert "stopped" in app._status_text.lower(), app._status_text
        ok(f"status says so: {app._status_text!r}")

        # the real test: nothing revives it after the fact
        for _ in range(12):
            await pilot.pause(0.25)
            assert app._activity is None, (
                f"spinner came back {app._activity!r} -- something in flight is "
                f"still reporting progress"
            )
            assert app._process_started is None
        ok(f"stayed stopped for 3s while {gateway.waves_sent} waves had been sent")

        assert not gateway.finished, "the gateway ran to completion anyway"
        ok("the member read was interrupted, not merely ignored")

        grew = len(app.rows) - rows_at_stop
        assert grew <= PER_WAVE, f"results grew by {grew} after stopping"
        ok(f"results stopped growing (+{grew} in-flight rows settled)")

        assert app._scan_task is None or app._scan_task.done()
        ok("the lookup task is cancelled, not orphaned")

        # and a fresh scan still works afterwards
        select_source(app, "guild")
        await pilot.pause(0.2)
        await pilot.press("s")
        for _ in range(40):
            await pilot.pause(0.1)
            if app._activity is not None:
                break
        assert app._activity is not None, "could not start a new scan after stopping"
        assert app._process_started is not None
        ok("a new scan starts cleanly after a stop")
        await pilot.press("x")
        await pilot.pause(0.3)

    print("\nALL STOP TESTS PASSED")


asyncio.run(main())
