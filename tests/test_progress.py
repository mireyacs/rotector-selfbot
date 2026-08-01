"""The status bar must never go quiet during a slow step.

Two properties are checked:

1. The gateway announces what it is about to do *before* it blocks on it.
   A channel with no member list costs a full FIRST_RESPONSE_TIMEOUT of
   silence, which is exactly when the UI used to look frozen.
2. The app turns those callbacks into a live activity label with an animated
   spinner, so even a genuine long wait visibly ticks.
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import rsb.discord.gateway as gw
from rsb.discord.http import Channel

ok = lambda m: print(f"[ok] {m}")


class DeafWS:
    """A socket that accepts sends and never answers -- the worst case."""

    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(payload)

    async def close(self):
        pass


async def test_gateway_announces_before_blocking():
    # Shrink every blocking constant so the test is quick; the property under
    # test is unchanged. All of them must be pinned here -- leaving one at its
    # production value silently reintroduces a multi-second gap.
    gw.FIRST_RESPONSE_TIMEOUT = 1.0
    gw.SETTLE_TIMEOUT = 0.3
    gw.CHUNK_TIMEOUT = 0.4
    gw.PREFIX_QUERY_DELAY = 0.05
    gw.MAX_ROUND_SECONDS = 3.0

    g = gw.DiscordGateway("fake")
    g._ws = DeafWS()

    events = []
    t0 = time.monotonic()

    def on_progress(found, total, note):
        events.append((time.monotonic() - t0, found, note))

    channels = [
        Channel(id="c1", name="general", type=0, position=0, everyone_can_view=True),
        Channel(id="c2", name="offtopic", type=0, position=1, everyone_can_view=True),
    ]
    # never resolves into members, so both channels time out and OP 8 also fails
    members = await g.fetch_members("g1", channels, expected=500, on_progress=on_progress)
    assert members == {}, members

    assert events, "no progress reported at all"
    first_at, _, first_note = events[0]
    # The property is "reported on the way in, not after the wait", so the
    # bound is a fraction of the blocking timeout rather than a fixed few
    # hundred milliseconds -- otherwise this doubles as a load detector and
    # fails for reasons that have nothing to do with the behaviour under test.
    assert first_at < gw.FIRST_RESPONSE_TIMEOUT / 2, (
        f"first progress took {first_at:.2f}s, which is not clearly before the "
        f"{gw.FIRST_RESPONSE_TIMEOUT}s blocking wait -- that is a silent gap"
    )
    assert "general" in first_note, first_note
    ok(f"first progress at {first_at * 1000:.0f}ms: {first_note!r}")

    # No silent gap may exceed the longest single blocking step, plus slack.
    longest_block = max(
        gw.FIRST_RESPONSE_TIMEOUT,
        gw.CHUNK_TIMEOUT + gw.PREFIX_QUERY_DELAY,
    )
    stamps = [e[0] for e in events]
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    worst = max(gaps) if gaps else 0.0
    assert worst < longest_block + 1.0, f"silent gap of {worst:.2f}s"
    ok(f"{len(events)} updates, worst gap {worst:.2f}s (longest blocking step is "
       f"{longest_block:.2f}s)")

    # Both channels named, then the OP 8 fallback announced.
    notes = " | ".join(n for _, _, n in events)
    assert "general" in notes and "offtopic" in notes, notes
    assert "OP 8" in notes, notes
    ok("every channel attempt and the OP 8 fallback are each announced")
    for at, _, note in events[:6]:
        print(f"       {at:5.2f}s  {note}")

    await g.close()


async def test_app_shows_live_activity():
    import rsb.tui.app as appmod
    from rsb.config import Config
    from rsb.discord.gateway import GuildMember
    from rsb.discord.http import Guild

    MEMBERS = {"1": GuildMember(id="1", username="u1")}

    class FakeHTTP:
        def __init__(self, token, **kw): pass
        async def me(self): return {"username": "t", "global_name": "T", "id": "9"}
        async def guilds(self):
            return [Guild(id="111", name="Slow Server", owner=False, permissions=0,
                          member_count=1, presence_count=1)]
        async def relationships(self): return []
        async def private_channels(self): return []
        async def channels(self, gid):
            await asyncio.sleep(0.4)          # a slow REST call
            return [Channel(id="c1", name="general", type=0, position=0,
                            everyone_can_view=True)]
        async def aclose(self): pass

    class SlowGateway:
        def __init__(self, token): self.user = None
        async def connect(self, timeout=45.0): return {}
        async def fetch_members(self, gid, channels, expected=None, on_progress=None,
                                on_members=None):
            for i in range(3):                 # a slow multi-step scrape
                if on_progress:
                    on_progress(i * 100, expected, f"Reading #general members {i}")
                if on_members and i == 0:
                    on_members(list(MEMBERS.values()))
                await asyncio.sleep(0.35)
            return dict(MEMBERS)
        async def close(self): pass

    appmod.DiscordHTTP = FakeHTTP
    appmod.DiscordGateway = SlowGateway

    cfg = Config(); cfg.token = "fake"
    app = appmod.ScannerApp(cfg)

    seen_labels, spinner_frames = [], set()

    original_set_activity = app._set_activity

    def record(text):
        if text not in seen_labels:
            seen_labels.append(text)
        original_set_activity(text)

    app._set_activity = record

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause(0.6)
        await pilot.press("s")
        for _ in range(40):                    # sample the bar while it works
            await pilot.pause(0.06)
            line = app._compose_status().plain
            if app._activity is not None:
                spinner_frames.add(line[0])
            if app._activity is None and seen_labels and app.rows:
                break

    assert len(seen_labels) >= 3, f"only saw {seen_labels}"
    ok(f"{len(seen_labels)} distinct activity labels during one scan")
    for label in seen_labels:
        print(f"       {label}")

    assert len(spinner_frames) > 1, f"spinner never animated: {spinner_frames}"
    ok(f"spinner animated through frames {sorted(spinner_frames)}")

    assert any("Reading channels" in s for s in seen_labels), seen_labels
    assert any("#general" in s for s in seen_labels), seen_labels
    assert any("Roblox" in s or "Discord accounts" in s for s in seen_labels), seen_labels
    ok("labels cover channel read, member fetch, and Rotector lookup stages")

    # settles onto a final, non-spinning message
    assert app._activity is None and "Scanned" in app._status_text, app._status_text
    ok(f"settles to a static summary: {app._status_text[:60]!r}")


async def main():
    await test_gateway_announces_before_blocking()
    print()
    await test_app_shows_live_activity()
    print("\nALL PROGRESS TESTS PASSED")


asyncio.run(main())
