"""Render SVG screenshots of the real UI for the README.

Runs the actual app headlessly against stub Discord data and a stub Rotector
client, so the images are genuine renders rather than drawings -- if the UI
changes, re-running this changes the pictures.

    python tools/screenshots.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import rsb.tui.app as appmod
from rsb.config import Config
from rsb.discord.gateway import GuildMember
from rsb.discord.http import GROUP_DM_CHANNEL, Channel, Guild, PrivateChannel
from rsb.proxy import ProbeResult
from rsb.rotector import MemberReport, RobloxAccount, TrackedServer
from rsb.tui.app import Row
from rsb.tui.proxies import ProxyTesterApp
from rsb.tui.settings import SettingsScreen

OUT = ROOT / "docs" / "screenshots"
SIZE = (128, 34)

NAMES = [
    ("kaidenz", "Kaiden", 2, 5, "Condo Activity",
     "[Trap3] Entered a condo game via unguessable entry code 8 time(s)"),
    ("mxrcy_", "mxrcy", 2, 1, "CSAM",
     "[ListData] Account present in known distribution list (2024-present)"),
    ("v0idkid", "v0id", 1, 2, "Sexual Content",
     "[Profile] Explicit solicitation in profile description"),
    ("sh4dowplay", "shadow", 5, 3, "Kink Content",
     "[Groups] Member of 3 fetish roleplay groups"),
    ("tinybunnie", "bunnie", 2, 5, "Condo Activity",
     "[Trap3] Entered a condo game via unguessable entry code 3 time(s)"),
    ("jayjay0417", "JayJay", 4, None, "", ""),
    ("rblxtrader99", "Trader", 6, None, "", ""),
    ("noodlearms", "noodle", 3, None, "", ""),
]


def make_rows() -> dict[str, Row]:
    rows: dict[str, Row] = {}
    for index, (username, nick, flag, category, reason, message) in enumerate(NAMES):
        report = MemberReport(discord_id=str(1000 + index))
        report.accounts.append(
            RobloxAccount(
                user_id=4_000_000 + index * 137,
                username=f"{username}_rblx",
                flag_type=flag,
                category=category,
                confidence=1.0,
                sources=[2],
                reasons={reason: {"message": message, "evidence": [
                    "Game: furre (v4 fork) (3 clicks)",
                    'Game: "prison life" (2 clicks)',
                ]}} if reason else {},
            )
        )
        if index % 3 == 0:
            report.servers.append(
                TrackedServer(f"s{index}", "condo hub", None, None, True, False)
            )
        member = GuildMember(id=str(1000 + index), username=username, nick=nick)
        rows[member.id] = Row(member=member, report=report)

    for extra in range(24):
        member = GuildMember(id=str(9000 + extra), username=f"member{extra:03d}")
        rows[member.id] = Row(
            member=member, report=MemberReport(discord_id=member.id)
        )
    return rows


class StubHTTP:
    def __init__(self, token, **kw): pass
    async def me(self):
        return {"username": "you", "global_name": "You", "id": "1"}
    async def guilds(self):
        return [
            Guild(id="1", name="Roblox Trading Hub", owner=False, permissions=0,
                  member_count=10833, presence_count=2140),
            Guild(id="2", name="Bloxburg Builders", owner=False, permissions=0,
                  member_count=4210, presence_count=380),
            Guild(id="3", name="Adopt Me Traders", owner=True, permissions=8,
                  member_count=1877, presence_count=210),
            Guild(id="4", name="art & commissions", owner=False, permissions=0,
                  member_count=642, presence_count=88),
        ]
    async def relationships(self):
        from rsb.discord.http import FRIEND, INCOMING_REQUEST, Relationship
        out = [
            Relationship(str(500 + i), f"friend{i}", None, "0", None, FRIEND)
            for i in range(14)
        ]
        out += [
            Relationship(str(600 + i), f"newperson{i}", None, "0", None,
                         INCOMING_REQUEST)
            for i in range(3)
        ]
        return out
    async def private_channels(self):
        return [
            PrivateChannel(id="g1", type=GROUP_DM_CHANNEL, name="art trades",
                           owner_id="1", recipients=[{"id": "700"}, {"id": "701"}]),
            PrivateChannel(id="g2", type=GROUP_DM_CHANNEL, name="commission chat",
                           owner_id="9", recipients=[{"id": "702"}]),
        ]
    async def channels(self, gid):
        return [Channel(id="c1", name="general", type=0, position=0,
                        everyone_can_view=True)]
    async def aclose(self): pass


class StubGateway:
    def __init__(self, token): self.user = None
    async def connect(self, timeout=45.0): return {}
    async def fetch_members(self, *a, **kw): return {}
    async def close(self): pass


async def shot(app, name: str, prepare, size=SIZE, settle=2.5):
    async with app.run_test(size=size) as pilot:
        # wait for the stub connect worker to finish populating sources
        for _ in range(int(settle / 0.1)):
            await pilot.pause(0.1)
            rows = getattr(app, "_source_rows", None)
            if rows or getattr(app, "entries", None):
                break
        await pilot.pause(0.4)
        await prepare(app, pilot)
        await pilot.pause(0.4)
        path = OUT / f"{name}.svg"
        app.save_screenshot(str(path))
        print(f"  wrote {path.relative_to(ROOT)}")


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    appmod.DiscordHTTP = StubHTTP
    appmod.DiscordGateway = StubGateway

    # must pass run_checks, or startup stops at the diagnostics screen.
    # Deliberately not token-shaped -- see the guard in test_units.py.
    STUB_TOKEN = "fake.test.token"

    def config():
        cfg = Config()
        cfg.token = STUB_TOKEN
        cfg.proxy.file = "/nonexistent"
        return cfg

    # 1. results, mid-scan feel
    async def results(app, pilot):
        app.rows.update(make_rows())
        app.current_source = app.sources[-1] if app.sources else None
        app.query_one("#results-title", appmod.Static).update(
            "RESULTS - Roblox Trading Hub (server)"
        )
        app._rebuild_table()
        app._set_status(
            "Scanned 10,833 members in 4m 12s - 5 flagged as THREAT.  "
            "Data: Rotector (https://rotector.com)",
            "bold red",
        )
        table = app.query_one("#results", appmod.DataTable)
        table.focus()
        table.move_cursor(row=0)
        await pilot.pause(0.3)

    await shot(appmod.ScannerApp(config()), "results", results)

    # 2. sources pane, grouped
    async def sources(app, pilot):
        app.query_one("#guilds", appmod.DataTable).focus()
        app._set_status("4 servers, 2 group DMs, friends and requests. Press s to scan.")
        await pilot.pause(0.3)
    await shot(appmod.ScannerApp(config()), "sources", sources)

    # 3. settings editor
    async def settings(app, pilot):
        app.push_screen(SettingsScreen(app.config))
        await pilot.pause(0.8)
    await shot(appmod.ScannerApp(config()), "settings", settings)

    # 4. proxy tester
    async def proxies(app, pilot):
        app.entries = ["direct", "23.95.150.11:6114", "198.23.239.134:6540",
                       "45.38.107.97:6014", "107.172.163.27:6543"]
        app._rebuild()
        results_map = {
            "direct": (True, 210.0, 200, 50, 49),
            "23.95.150.11:6114": (True, 340.0, 200, 50, 49),
            "198.23.239.134:6540": (True, 512.0, 200, 50, 44),
            "45.38.107.97:6014": (False, None, None, None, None),
            "107.172.163.27:6543": (True, 288.0, 403, None, None),
        }
        for entry, (ok_, latency, status, limit, remaining) in results_map.items():
            result = ProbeResult(raw=entry, url=None, label=entry, ok=ok_)
            result.latency_ms = latency
            result.status = status
            result.rate_limit = limit
            result.rate_remaining = remaining
            if not ok_:
                result.error = "ConnectError: All connection attempts failed"
            elif status == 403:
                result.notes.append("Rotector refused this exit IP")
            elif remaining is not None and limit - remaining > 1:
                result.notes.append(
                    f"{limit - remaining - 1} other request(s) already on this "
                    f"exit's window - shares a budget"
                )
            else:
                result.notes.append(f"own budget: {limit}/window")
            app.results[entry] = result
        app._rebuild()
        app._refresh_head()
        app._set_status(
            "Tested 5 - 2 with their own budget, 1 shared, 1 dead. "
            "Combined 100 req/window."
        )
        app.query_one("#table", appmod.DataTable).focus()
        await pilot.pause(0.3)

    cfg = config()
    await shot(ProxyTesterApp(cfg), "proxies", proxies)

    print(f"\n{len(list(OUT.glob('*.svg')))} screenshots in {OUT.relative_to(ROOT)}")


asyncio.run(main())
