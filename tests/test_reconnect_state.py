"""The status bar must not stay stuck on "reconnecting" after reconnecting.

The reported symptom was a bar reading

    / Gateway session invalidated - reconnecting in 1s (attempt 1);
      the scan will continue    task 40m 04s

on a connection that had been working the whole time -- scanning another server
succeeded immediately. The gateway announced the drop and never announced the
recovery, and an *activity* is only cleared by something else finishing, so the
spinner and its task timer ran for as long as the app stayed open.

Two separate faults, both covered here: the missing recovery callback, and a
cancelled scan skipping _end_run because the handler that called it sat below
`except CancelledError: raise` and was unreachable.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import rsb.tui.app as appmod
from rsb.config import Config
from rsb.discord.gateway import DiscordGateway
from rsb.discord.http import Channel, Guild
from rsb.tui.app import ScannerApp

ok = lambda m: print(f"[ok] {m}")


class _StubHTTP:
    def __init__(self, token, **kw):
        pass

    async def me(self):
        return {"username": "you", "global_name": "You", "id": "1"}

    async def guilds(self):
        return [Guild(id="1", name="g", owner=False, permissions=0,
                      member_count=10, presence_count=2)]

    async def relationships(self):
        return []

    async def private_channels(self):
        return []

    async def channels(self, gid):
        return [Channel(id="c", name="general", type=0, position=0,
                        everyone_can_view=True)]

    async def aclose(self):
        pass


class _StubGateway:
    def __init__(self, token, bot=False):
        self.user = None
        self.on_reconnect = None
        self.on_reconnected = None

    async def connect(self, timeout=45.0):
        return {}

    async def fetch_members(self, *a, **kw):
        return {}

    async def close(self):
        pass


# --- the gateway announces recovery, once, and only when it is news --------

async def _gateway_announces_recovery():
    gateway = DiscordGateway("t")
    fired = []
    gateway.on_reconnected = lambda resumed: fired.append(resumed)

    await gateway._dispatch("READY", {"user": {"id": "1"}, "session_id": "s"})
    assert fired == [], "the first READY is not a reconnection"

    # what _read_loop's finally leaves behind, plus the supervisor's counter
    gateway._connected.clear()
    gateway.reconnects = 1
    await gateway._dispatch("RESUMED", {})
    assert fired == [True], fired

    await gateway._dispatch("RESUMED", {})
    assert fired == [True], "an already-connected session is not news again"

    gateway._connected.clear()
    gateway.reconnects = 2
    await gateway._dispatch("READY", {"user": {"id": "1"}, "session_id": "s2"})
    assert fired == [True, False], f"a fresh session reports resumed=False: {fired}"
    ok("on_reconnected fires once per reconnect, and not on the first READY")

    # a callback that raises is a display problem, not a link problem
    gateway._connected.clear()
    gateway.reconnects = 3
    gateway.on_reconnected = lambda r: (_ for _ in ()).throw(RuntimeError("boom"))
    await gateway._dispatch("RESUMED", {})
    assert gateway._connected.is_set(), "a raising callback took the session down"
    ok("a callback that raises does not break the connection")


# --- and the app clears the notice when it fires ---------------------------

async def _app_recovers_the_status_bar():
    appmod.DiscordHTTP = _StubHTTP
    appmod.DiscordGateway = _StubGateway

    config = Config()
    config.token = "fake.test.token"
    config.proxy.file = "/nonexistent"
    app = ScannerApp(config, persist_theme=False)

    async with app.run_test(size=(120, 40)) as pilot:
        for _ in range(50):
            await pilot.pause(0.1)
            if app.rotector is not None and app.gateway is not None:
                break
        assert app.gateway is not None, "never connected"
        assert app.gateway.on_reconnected is not None, "recovery is not wired up"

        # idle: a drop must not claim a scan is continuing
        app.gateway.on_reconnect(1, 1.0, "session invalidated")
        await pilot.pause(0.3)
        assert app._activity and "scan will continue" not in app._activity, app._activity
        ok("an idle reconnect does not promise to continue a scan that is not running")

        app.gateway.on_reconnected(False)
        await pilot.pause(0.3)
        assert app._activity is None, f"still spinning: {app._activity!r}"
        assert "reconnect" in app._status_text.lower(), app._status_text
        line = app._compose_status().plain
        assert "reconnecting in" not in line, f"bar still stuck: {line!r}"
        assert "task " not in line, f"a settled state must not run a task timer: {line!r}"
        ok("recovery clears the activity, the spinner and the task timer")

        # mid-scan: the wording differs and the bar hands back to the scan
        app._process_started = 1.0
        assert app._scanning
        app.gateway.on_reconnect(1, 1.0, "session invalidated")
        await pilot.pause(0.2)
        assert "scan will continue" in (app._activity or ""), app._activity
        app.gateway.on_reconnected(True)
        await pilot.pause(0.2)
        assert app._activity and "resuming the scan" in app._activity, app._activity
        ok("mid-scan the notice names the scan and then hands the bar back")

        # a cancelled run must leave nothing animating
        app._process_started = 1.0
        app._set_activity("Reading the member list")
        assert app._activity is not None
        app._end_run()
        assert app._activity is None and app._process_started is None
        assert not app._scanning
        ok("_end_run clears the run, which the cancel path now always reaches")


asyncio.run(_gateway_announces_recovery())
asyncio.run(_app_recovers_the_status_bar())

# --- the unreachable-handler shape must not come back ---------------------
# `except CancelledError: raise` followed by a second CancelledError handler
# silently makes the second one dead code, which is exactly how the cleanup
# got skipped.
source = (Path(__file__).resolve().parent.parent / "rsb/tui/app.py").read_text(encoding="utf-8")
scan = source[source.index("async def scan_source"):]
scan = scan[: scan.index("\n    # -- results table")]
assert scan.count("except asyncio.CancelledError") == 1, (
    "scan_source has more than one CancelledError handler; the later one is "
    "unreachable and its cleanup will never run"
)
assert "self._end_run()" in scan, "scan_source must end the run in its finally"
ok("scan_source has exactly one CancelledError handler and ends the run")

print("\nall reconnect state checks passed.")
