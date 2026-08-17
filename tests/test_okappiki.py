"""Okappiki backend: parsing, verdict tiering, and the backend switch.

Every body below is one this endpoint was actually observed returning, kept
verbatim. The API is undocumented, so these recordings are the only
specification there is -- if the service changes shape, this is where it should
be noticed rather than in a scan that quietly reports nobody as flagged.

Offline: nothing here touches the network.
"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rsb.backend import ATTRIBUTIONS, backend_label, build_backend
from rsb.config import BACKENDS, Config, write_section
from rsb.okappiki import (
    MIN_SNOWFLAKE_DIGITS,
    OkappikiClient,
    SOURCES,
    normalise_id,
    parse_report,
    parse_signals,
)
from rsb.rotector import RotectorClient
from rsb.verdict import Verdict

ok = lambda m: print(f"[ok] {m}")

# --- recorded responses ---------------------------------------------------

ALL_FLAGGED = json.loads(r"""
{"success":true,"discord_id":"1212568604604629013",
 "okappiki":{"flagged":true,"reason":"Basement Hub, prime society, Condo Links",
             "roblox_username":null,"roblox_id":null},
 "rotector":{"flagged":true,"roblox_id":9092847610,
             "username":"iskidfromgithubrepos","flag_type":2,
             "reason":"[Condo Activity] [Discord] detected in 13+ condo servers"},
 "mococo":{"flagged":true,"score_sum":40,"reason":"Detected in: Basement Hub"},
 "last_updated":"2026-08-03T08:21:58+00:00"}
""")

NONE_FLAGGED = json.loads(
    '{"success":true,"discord_id":"80351110224678912",'
    '"okappiki":{"flagged":false},"rotector":{"flagged":false},'
    '"mococo":{"flagged":false},"last_updated":"2026-08-03T08:22:51+00:00"}'
)

REFUSED = json.loads('{"success":false,"message":"Invalid Discord ID"}')

# --- verdict tiering ------------------------------------------------------
# The rule the whole integration turns on: only Rotector publishes flag types
# with documented actionability, so only Rotector's finding may reach THREAT.

report = parse_report("1212568604604629013", ALL_FLAGGED)
assert report.verdict is Verdict.THREAT, report.verdict
by_source = {s.source: s for s in report.signals}
assert by_source["rotector"].verdict is Verdict.THREAT
assert by_source["okappiki"].verdict is Verdict.CAUTION, "unverified must not be THREAT"
assert by_source["mococo"].verdict is Verdict.CAUTION, "unverified must not be THREAT"
assert by_source["mococo"].score == 40
ok(f"three sources flagged -> {report.verdict.name}, unverified capped at CAUTION")

# an Okappiki-only sighting must not present as actionable
only_okappiki = {
    "success": True,
    "okappiki": {"flagged": True, "reason": "seen in a condo server"},
    "rotector": {"flagged": False},
    "mococo": {"flagged": False},
}
assert parse_report("1", only_okappiki).verdict is Verdict.CAUTION
ok("okappiki-only sighting -> CAUTION, never THREAT")

clean = parse_report("80351110224678912", NONE_FLAGGED)
assert clean.verdict is Verdict.NO_DETECTIONS, clean.verdict
assert clean.accounts == [] and len(clean.signals) == 3
assert clean.flagged_signals == []
ok("nothing flagged -> NO DETECTIONS with all three sources on record")

# a Rotector flag type that is *not* actionable must not be promoted
queued = {"success": True, "rotector": {"flagged": True, "flag_type": 3}}
assert parse_report("1", queued).verdict is Verdict.INFO, "flag type 3 is not a threat"
ok("non-actionable Rotector flag type stays INFO, per verdict.py's rules")

# --- a source that did not answer is not a source that said "no" ----------
partial = {"success": True, "okappiki": {"flagged": False}}
signals = parse_signals(partial)
assert [s.source for s in signals] == ["okappiki"], signals
missing = [s for s in SOURCES if s not in {g.source for g in signals}]
assert set(missing) == {"rotector", "mococo"}
ok("absent sources are omitted, not recorded as unflagged")

# --- accounts are built where a source names one --------------------------
assert len(report.accounts) == 1, report.accounts
assert report.accounts[0].user_id == 9092847610
assert report.accounts[0].username == "iskidfromgithubrepos"
assert report.accounts[0].flag_type == 2
assert clean.accounts == []
ok("a source naming a Roblox account yields one the results table can show")

# --- id normalisation -----------------------------------------------------
# The endpoint's own sub-lookups disagree about leading zeros: the padded form
# returned Rotector's record but not Okappiki's. Normalising before the request
# is what makes the two agree.
assert normalise_id("00001212568604604629013") == "1212568604604629013"
assert normalise_id(" 80351110224678912 ") == "80351110224678912"
assert normalise_id(1212568604604629013) == "1212568604604629013"
# and ids that are not snowflakes are refused rather than asked about: the
# endpoint answers for "1" with a confident-looking Rotector record
for junk in ("1", "notanid", "", "12345", "-1", "1.0"):
    assert normalise_id(junk) is None, junk
assert normalise_id("9" * MIN_SNOWFLAKE_DIGITS) is not None
ok("ids normalised; short and non-numeric ids refused before any request")


# --- a refusal becomes an error, never a clean result ---------------------
class _FakeResponse:
    status_code = 200
    headers: dict = {}

    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


class _FakeRoute:
    name = "direct"
    is_direct = True

    def __init__(self, body, limiter):
        self._body = body
        self.limiter = limiter
        self.client = self

    async def get(self, _path, params=None):
        return _FakeResponse(self._body)

    def penalise(self, *_a):
        pass

    def recover(self):
        pass


async def _refusal_becomes_error():
    client = OkappikiClient()
    route = _FakeRoute(REFUSED, client.limiter)
    client.pool.pick = lambda exclude=None: route
    try:
        report = await client.lookup_member("1212568604604629013")
        assert report.error and "Invalid Discord ID" in report.error, report.error
        assert report.verdict is Verdict.UNKNOWN
        assert not report.signals, "a refusal must not look like 'nothing found'"
        assert client.count_unanswered({"x": report}) == 1
    finally:
        await client.aclose()


asyncio.run(_refusal_becomes_error())
ok("a success:false body (served as HTTP 200) becomes an unanswered report")


# --- the switch itself ----------------------------------------------------
def _config(backend: str) -> Config:
    config = Config()
    config.token = "x"
    config.scan.backend = backend
    return config


assert isinstance(build_backend(_config("rotector"), []), RotectorClient)
assert isinstance(build_backend(_config("okappiki"), []), OkappikiClient)
assert set(BACKENDS) == {"rotector", "okappiki"}
ok("scan.backend selects the client; both names build")

bad = _config("rotektor")
assert bad.validate(), "a misspelled backend must fail validation"
try:
    build_backend(bad, [])
except ValueError:
    ok("an unknown backend raises rather than silently falling back to Rotector")
else:
    raise AssertionError("build_backend accepted an unknown backend")

# both clients must offer everything the app reaches for
SURFACE = (
    "scan_stream", "scan_members", "capacity_units_per_sec", "estimate_seconds",
    "purge_cache", "aclose", "lookup_discord_user_detail", "count_unanswered",
    "limiter", "pool",
)
for backend in BACKENDS:
    client = build_backend(_config(backend), [])
    missing = [name for name in SURFACE if not hasattr(client, name)]
    assert not missing, f"{backend} is missing {missing}"
ok(f"both backends expose all {len(SURFACE)} members the app uses")

# the ETA has to reflect that Okappiki cannot batch, or a scan lies about
# taking a minute when it takes half an hour
rot = build_backend(_config("rotector"), []).estimate_seconds(10833)
okp = build_backend(_config("okappiki"), []).estimate_seconds(10833)
assert rot and okp and okp > rot * 20, (rot, okp)
assert build_backend(_config("okappiki"), []).estimate_seconds(0) is None
ok(f"ETA is backend-aware: {rot / 60:.0f} min batched vs {okp / 60:.0f} min one-by-one")

# The ETA is bounded by requests-in-flight, not by the rate limiter. The limiter
# is deliberately configured above what the client can produce (measurement puts
# the endpoint's knee at 4-5 concurrent, ~5.5 req/s), so an ETA that believed it
# would promise a scan that never arrives.
loose = _config("okappiki")
loose.okappiki.rate_limit = 500
assert build_backend(loose, []).estimate_seconds(10833) == okp, (
    "raising the rate limit alone must not change the ETA -- concurrency binds"
)

wider = _config("okappiki")
wider.okappiki.concurrency = 8
assert build_backend(wider, []).estimate_seconds(10833) < okp
ok("the ETA follows concurrency rather than the rate limit, which is the real ceiling")

assert backend_label("okappiki") == "Okappiki"
assert "okappiki.com" in ATTRIBUTIONS["okappiki"]
assert "rotector.com" in ATTRIBUTIONS["rotector"]
ok("each backend carries its own attribution")

# --- the [ui] theme round-trips through config.toml ------------------------
with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp) / "config.toml"
    path.write_text('[discord]\ntoken = "x"\n\n[ui]\ntheme = ""\n', encoding="utf-8")
    write_section(path, "ui", {"theme": "ten-thousand"})
    restored = Config.load(path)
    assert restored.ui.theme == "ten-thousand", restored.ui.theme
    assert restored.token == "x", "writing one section must not disturb another"
ok("theme survives a write/read round trip without touching [discord]")

# --- switching from the TUI actually swaps the client ---------------------
# The failure this guards against is silent: the header and the attribution
# read the backend off the config, so a save that updated the config but not
# the client would have the UI claiming Okappiki while every lookup still went
# to Rotector.
import rsb.tui.app as appmod  # noqa: E402
from rsb.discord.gateway import GuildMember  # noqa: E402
from rsb.discord.http import Channel, Guild  # noqa: E402
from rsb.migrate import SCHEMA  # noqa: E402
from rsb.rotector import MemberReport  # noqa: E402
from rsb.tui.app import Row, ScannerApp  # noqa: E402


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

    async def connect(self, timeout=45.0):
        return {}

    async def fetch_members(self, *a, **kw):
        return {}

    async def close(self):
        pass


async def _switch_from_the_ui():
    appmod.DiscordHTTP = _StubHTTP
    appmod.DiscordGateway = _StubGateway

    config = _config("rotector")
    config.token = "fake.test.token"
    config.proxy.file = "/nonexistent"
    app = ScannerApp(config, persist_theme=False)

    async with app.run_test(size=(120, 40)) as pilot:
        for _ in range(40):
            await pilot.pause(0.1)
            if app.rotector is not None:
                break
        assert isinstance(app.rotector, RotectorClient), type(app.rotector)
        assert "Rotector" in app.sub_title, app.sub_title
        ok(f"opens on the configured backend: {app.sub_title!r}")

        app.rows["1"] = Row(
            member=GuildMember(id="1", username="a"),
            report=MemberReport(discord_id="1"),
        )
        config.scan.backend = "okappiki"
        app.switch_backend("rotector")
        for _ in range(60):
            await pilot.pause(0.1)
            if isinstance(app.rotector, OkappikiClient):
                break

        assert isinstance(app.rotector, OkappikiClient), type(app.rotector)
        assert "Okappiki" in app.sub_title and "API key" not in app.sub_title
        assert app.backend_name == "Okappiki"
        assert "okappiki.com" in app.attribution
        assert not app.rows, "results from the previous backend must not survive"
        ok(f"switching swaps the client, the header and the attribution together")

        # a hand-edited config can still hold a typo; it must not leave the app
        # with no backend at all
        config.scan.backend = "rotektor"
        app.switch_backend("okappiki")
        for _ in range(60):
            await pilot.pause(0.1)
            if config.scan.backend == "okappiki":
                break
        assert isinstance(app.rotector, OkappikiClient), type(app.rotector)
        assert config.scan.backend == "okappiki", "should revert to what worked"
        ok("an invalid backend reverts and leaves a usable client in place")


asyncio.run(_switch_from_the_ui())

# and the settings screen offers the valid names rather than a free-text box
backend_setting = next(
    setting
    for section in SCHEMA if section.name == "scan"
    for setting in section.settings if setting.name == "backend"
)
assert backend_setting.choices == BACKENDS, backend_setting.choices
ok("the settings screen renders scan.backend as a dropdown of the valid names")

print("\nall okappiki backend checks passed.")
