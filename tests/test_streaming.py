"""Results must populate while the member list is still being read.

The gateway is stubbed to reveal members in slow waves, the way a real sidebar
scrape does. The Rotector half is the live API. What is asserted is ordering:
rows must exist in the table *before* the member list has finished loading.
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
from rsb.verdict import Verdict

ok = lambda m: print(f"[ok] {m}")

WAVES = 3
PER_WAVE = 100
WAVE_GAP = 0.7

# ids Rotector actually has findings for, seeded into the first wave
SEEDED = ["1", "2", "3", "4", "5"]


def wave(index: int) -> list[GuildMember]:
    members = []
    if index == 0:
        members += [GuildMember(id=i, username=f"seed{i}") for i in SEEDED]
    base = 900_000_000_000_000_000 + index * PER_WAVE
    members += [
        GuildMember(id=str(base + n), username=f"u{index}_{n}")
        for n in range(PER_WAVE - len(members))
    ]
    return members


class StreamingGateway:
    """Reveals members in waves, like a real member-list scrape."""

    def __init__(self, token):
        self.user = None
        self.emitted_at: list[float] = []
        self.finished_at: float | None = None
        self.all: dict[str, GuildMember] = {}

    async def connect(self, timeout=45.0):
        return {}

    async def fetch_members(
        self, gid, channels, expected=None, on_progress=None, on_members=None
    ):
        for index in range(WAVES):
            batch = wave(index)
            for member in batch:
                self.all[member.id] = member
            if on_progress:
                on_progress(len(self.all), expected, "Reading #general member list")
            if on_members:
                on_members(batch)
            self.emitted_at.append(time.monotonic())
            await asyncio.sleep(WAVE_GAP)
        self.finished_at = time.monotonic()
        return dict(self.all)

    async def close(self):
        pass


class FakeHTTP:
    def __init__(self, token, **kw): pass
    async def me(self): return {"username": "t", "global_name": "T", "id": "9"}
    async def guilds(self):
        return [Guild(id="111", name="Streaming Server", owner=False, permissions=0,
                      member_count=WAVES * PER_WAVE, presence_count=10)]
    async def channels(self, gid):
        return [Channel(id="c1", name="general", type=0, position=0,
                        everyone_can_view=True)]
    async def aclose(self): pass


async def main():
    appmod.DiscordHTTP = FakeHTTP
    appmod.DiscordGateway = StreamingGateway

    cfg = Config()
    cfg.token = "fake"
    app = appmod.ScannerApp(cfg)

    first_row_at = None
    first_finding_at = None
    timeline = []
    saw_reading_label = False

    async with app.run_test(size=(130, 36)) as pilot:
        await pilot.pause(0.8)
        gateway = app.gateway
        await pilot.press("s")

        started = time.monotonic()
        for _ in range(300):
            await pilot.pause(0.1)
            now = time.monotonic()
            if app.rows and first_row_at is None:
                first_row_at = now
            table = app.query_one("#results", appmod.DataTable)
            if table.row_count and first_finding_at is None:
                first_finding_at = now
            if app._activity and "still reading" in app._activity:
                saw_reading_label = True
            timeline.append((now - started, len(app.rows)))
            if gateway.finished_at and not app._activity:
                break

        total_expected = WAVES * PER_WAVE
        assert len(app.rows) == total_expected, f"{len(app.rows)} of {total_expected}"
        ok(f"all {total_expected} members scanned")

        assert first_row_at is not None, "no rows ever appeared"
        assert gateway.finished_at is not None, "gateway never finished"

        lead = gateway.finished_at - first_row_at
        assert lead > 0, (
            f"first row appeared {-lead:.2f}s AFTER reading finished -- "
            f"results are still batched to the end"
        )
        ok(f"first results appeared {lead:.2f}s BEFORE the member list finished "
           f"loading")

        # rows must be present while waves are still arriving
        at_last_wave = next(
            (n for t, n in timeline if t + started >= gateway.emitted_at[-1]), 0
        )
        assert at_last_wave > 0, "nothing scanned by the time the last wave arrived"
        ok(f"{at_last_wave} members already scanned when the final wave arrived")

        assert first_finding_at is not None
        assert first_finding_at < gateway.finished_at
        ok(f"a finding was listed {gateway.finished_at - first_finding_at:.2f}s "
           f"before reading finished")

        assert saw_reading_label, "status never said the read was still ongoing"
        ok("status bar flags that reading is still in progress during the scan")

        threats = [r for r in app.rows.values() if r.report.verdict is Verdict.THREAT]
        assert threats, "seeded threat ids produced no THREAT verdict"
        ok(f"{len(threats)} THREAT verdict(s) from the seeded ids")

        errored = [r for r in app.rows.values() if r.report.error]
        assert not errored, f"{len(errored)} unanswered while streaming"
        ok("no member went unanswered in the streamed pipeline")

        progression = [n for _, n in timeline]
        assert progression[0] < progression[-1]
        steps = sorted({n for n in progression if n})
        ok(f"row count grew progressively: {steps[:6]}{'...' if len(steps) > 6 else ''}")

    print("\nALL STREAMING TESTS PASSED")


asyncio.run(main())
