"""Running as a bot application instead of a user account.

The two are not the same client wearing different hats. A user token has to
imitate the real client down to `x-super-properties` or routes answer 403; a
bot must not do that, has to declare intents up front, and has no friends,
DMs or group DMs at all. What it gets in exchange is the one thing a user
account genuinely cannot have: every member of a server, offline included,
without needing kick/ban permissions.

Nothing here touches the network.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import rsb.discord.gateway as gwmod
from rsb.config import Config
from rsb.discord.gateway import BOT_INTENTS, INTENT_HELP, DiscordGateway
from rsb.discord.http import (
    BOT_UA,
    BROWSER_UA,
    BotUnsupported,
    Channel,
    DiscordHTTP,
)
from rsb.tui.settings import run_checks

ok = lambda m: print(f"[ok] {m}")


# --------------------------------------------------------------------------
# which token wins
# --------------------------------------------------------------------------


def test_user_token_wins():
    both = Config(token="user.tok.en", bot_token="bot.tok.en")
    assert not both.is_bot and both.active_token == "user.tok.en"
    ok("with both set, the user token is used -- it can do strictly more")

    bot_only = Config(bot_token="bot.tok.en")
    assert bot_only.is_bot and bot_only.active_token == "bot.tok.en"
    ok("with only a bot token, it runs as a bot")

    user_only = Config(token="user.tok.en")
    assert not user_only.is_bot
    ok("with only a user token, nothing changes")

    assert Config().validate(), "no token at all should still be a problem"
    assert "DISCORD_BOT_TOKEN" in Config().validate()[0]
    ok("with neither, the error names both ways to supply one")

    blank = Config(token="   ", bot_token="bot.tok.en")
    assert blank.is_bot, "whitespace counted as a user token"
    ok("a blank user token does not shadow a real bot token")

    names = [c.name for c in run_checks(both)]
    assert "Both tokens set" in names
    ok("and the diagnostics say outright that the bot token is being ignored")


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


def test_http_identifies_correctly():
    user = DiscordHTTP("user.tok.en")
    bot = DiscordHTTP("bot.tok.en", bot=True)

    assert user._http.headers["authorization"] == "user.tok.en"
    assert bot._http.headers["authorization"] == "Bot bot.tok.en"
    ok("a bot token is sent with the 'Bot ' prefix, a user token bare")

    assert "x-super-properties" in user._http.headers
    assert "x-super-properties" not in bot._http.headers
    ok("the client-imitation headers are sent only for a user token")

    assert user._http.headers["user-agent"] == BROWSER_UA
    assert bot._http.headers["user-agent"] == BOT_UA
    assert "DiscordBot" in BOT_UA
    ok(f"and a bot identifies honestly: {BOT_UA}")

    assert user.is_bot is False and bot.is_bot is True


async def test_user_only_routes_are_refused():
    bot = DiscordHTTP("bot.tok.en", bot=True)
    calls = [
        ("relationships", bot.relationships()),
        ("private_channels", bot.private_channels()),
        ("remove_friend", bot.remove_friend("1")),
        ("block_user", bot.block_user("1")),
        ("leave_group_dm", bot.leave_group_dm("1")),
        ("remove_group_recipient", bot.remove_group_recipient("1", "2")),
    ]
    for name, coro in calls:
        try:
            await coro
            raise AssertionError(f"{name} was allowed for a bot")
        except BotUnsupported:
            pass
    ok(f"all {len(calls)} user-only routes refuse a bot token up front, "
       f"rather than issuing a request that would 403")
    await bot.aclose()

    user = DiscordHTTP("user.tok.en")
    try:
        await user.list_guild_members("1")
        raise AssertionError("a user token was allowed the bot member route")
    except BotUnsupported:
        ok("and the bot-only member route refuses a user token, symmetrically")
    await user.aclose()


async def test_member_pagination():
    """The REST list is paged by ascending id, not by page number."""
    bot = DiscordHTTP("bot.tok.en", bot=True)
    pages = {
        "0": [{"user": {"id": str(1000 + i)}} for i in range(1000)],
        "1999": [{"user": {"id": str(2000 + i)}} for i in range(1000)],
        "2999": [{"user": {"id": str(3000 + i)}} for i in range(120)],
    }
    asked: list[str] = []

    async def fake_list(guild_id, limit=1000, after="0"):
        asked.append(after)
        return pages.get(after, [])

    bot.list_guild_members = fake_list
    streamed: list[int] = []
    members = await bot.all_guild_members(
        "111", on_members=lambda page: streamed.append(len(page)), page_delay=0
    )

    assert len(members) == 2120, len(members)
    ok(f"paged through {len(members):,} members in {len(asked)} requests")
    assert asked == ["0", "1999", "2999"], asked
    ok(f"each request asked after the highest id seen: {asked}")
    assert streamed == [1000, 1000, 120]
    ok("and each page was handed on as it arrived, not held to the end")

    ids = {m["user"]["id"] for m in members}
    assert len(ids) == len(members), "pagination repeated members"
    ok("with no duplicates and no gap between pages")
    await bot.aclose()


# --------------------------------------------------------------------------
# gateway
# --------------------------------------------------------------------------


class StubSocket:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        import json
        self.sent.append(json.loads(payload))

    async def close(self):
        pass


async def test_identify_declares_intents():
    bot = DiscordGateway("bot.tok.en", bot=True)
    socket = StubSocket()
    bot._ws = socket
    bot._socket_ready.set()
    await bot._identify()

    payload = socket.sent[0]["d"]
    assert payload["token"] == "bot.tok.en"
    assert payload["intents"] == BOT_INTENTS == 3, payload.get("intents")
    ok(f"a bot IDENTIFYs with intents={BOT_INTENTS} "
       f"(GUILDS | GUILD_MEMBERS), which is all a member scan needs")

    assert "capabilities" not in payload and "client_state" not in payload
    assert "browser_user_agent" not in payload["properties"]
    ok("and does not carry the web-client impersonation a user token needs")

    user = DiscordGateway("user.tok.en")
    socket2 = StubSocket()
    user._ws = socket2
    user._socket_ready.set()
    await user._identify()
    upayload = socket2.sent[0]["d"]
    assert "intents" not in upayload
    assert "capabilities" in upayload
    ok("while the user path is untouched: no intents, capabilities intact")


async def test_missing_intent_is_explained():
    """4014 is the one failure a bot operator will actually hit."""
    from rsb.discord.gateway import FATAL_CLOSE_CODES

    assert 4014 in FATAL_CLOSE_CODES and 4013 in FATAL_CLOSE_CODES
    assert "Developer Portal" in INTENT_HELP
    assert "SERVER MEMBERS INTENT" in INTENT_HELP
    ok("a disallowed-intent close carries the steps to fix it, "
       "not just 'disallowed intents'")


async def test_bot_never_uses_the_sidebar():
    """OP 14 is a client feature. A bot asking for it would be nonsense."""
    bot = DiscordGateway("bot.tok.en", bot=True)
    sent: list[dict] = []
    scraped: list = []

    async def fake_send(payload, metered=True):
        sent.append(payload)

    async def fake_request_members(guild_id, expected, on_progress, members,
                                   emit, **kwargs):
        assert kwargs.get("open_query_only") is True, kwargs
        for i in range(50):
            members[str(i)] = gwmod.GuildMember(id=str(i), username=f"u{i}")
        emit()
        return members

    async def fake_scrape(*a, **kw):
        scraped.append(a)
        return {}

    bot._send = fake_send
    bot._check_alive = lambda: None
    bot._request_members = fake_request_members
    bot._scrape_sidebar = fake_scrape

    channels = [Channel(id="c1", name="general", type=0, position=0,
                        everyone_can_view=True)]
    got = await bot.fetch_members("111", channels, expected=50)

    assert len(got) == 50, len(got)
    ok(f"the bot path got all {len(got)} members straight from the chunk route")
    assert not scraped, "a bot tried to scrape the member sidebar"
    ok("the sidebar scrape was never attempted")
    assert not any(p.get("op") == 14 for p in sent), sent
    ok("and no OP 14 guild subscription was ever sent")


async def test_bot_does_not_need_permissions_to_chunk():
    """The kick/ban gate is a user-account limitation, not a bot one."""
    from rsb.discord.http import Guild

    powerless = Guild(id="111", name="Srv", owner=False, permissions=0,
                      member_count=5000, presence_count=100)
    assert not powerless.can_chunk
    ok("this guild grants the account no kick/ban/manage-roles...")

    bot = DiscordGateway("bot.tok.en", bot=True)
    used: dict = {}

    async def fake_request_members(guild_id, expected, on_progress, members,
                                   emit, **kwargs):
        used["called"] = True
        members["1"] = gwmod.GuildMember(id="1", username="someone")
        emit()
        return members

    bot._send = lambda *a, **kw: asyncio.sleep(0)
    bot._check_alive = lambda: None
    bot._request_members = fake_request_members
    bot._scrape_sidebar = lambda *a, **kw: asyncio.sleep(0)

    # can_chunk deliberately left False, as the permissions imply
    await bot.fetch_members("111", [], expected=5000, can_chunk=False)
    assert used.get("called"), "the bot did not even try the full member list"
    ok("...and the bot requests the full member list regardless, "
       "because the intent is what grants it")


# --------------------------------------------------------------------------
# the app
# --------------------------------------------------------------------------


class FakeBotHTTP:
    """A bot token's view: guilds, and refusal for everything personal."""

    def __init__(self, token, bot=False, **kw):
        self.is_bot = bot
        self.asked: list[str] = []

    async def me(self):
        self.asked.append("me")
        return {"username": "scanner", "global_name": "Scanner",
                "id": "42", "bot": True}

    async def guilds(self):
        from rsb.discord.http import Guild
        self.asked.append("guilds")
        return [Guild(id="111", name="Srv", owner=False, permissions=0,
                      member_count=4000, presence_count=90)]

    async def relationships(self):
        self.asked.append("relationships")
        raise BotUnsupported("bots have no friends")

    async def private_channels(self):
        self.asked.append("private_channels")
        raise BotUnsupported("bots have no DMs")

    async def widget(self, gid):
        return None

    async def aclose(self):
        pass


class FakeBotGateway:
    def __init__(self, token, bot=False):
        self.user = None
        self.is_bot = bot
        self.on_reconnect = None

    async def connect(self, timeout=45.0):
        return {}

    async def close(self):
        pass


async def test_app_runs_as_a_bot():
    import rsb.tui.app as appmod

    appmod.DiscordHTTP = FakeBotHTTP
    appmod.DiscordGateway = FakeBotGateway

    cfg = Config()
    cfg.bot_token = "fake.test.token"
    app = appmod.ScannerApp(cfg)

    async with app.run_test(size=(140, 40)) as pilot:
        for _ in range(60):
            await pilot.pause(0.1)
            if app.sources:
                break

        assert app.is_bot, "the app did not pick up bot mode"
        ok("the app authenticated as a bot")

        kinds = {s.kind for s in app.sources}
        assert kinds == {"guild"}, kinds
        ok(f"only servers are offered as sources: {sorted(kinds)}")

        assert "relationships" not in app.http.asked
        assert "private_channels" not in app.http.asked
        ok("friends and DMs were not even requested -- no pointless 403s")

        assert "[bot]" in (app.sub_title or "")
        ok(f"and the mode is visible in the title: {app.sub_title!r}")


async def test_a_bot_token_in_the_user_field_is_corrected():
    """Pasting it into the wrong slot should not fail three steps later."""
    import rsb.tui.app as appmod

    appmod.DiscordHTTP = FakeBotHTTP
    appmod.DiscordGateway = FakeBotGateway

    cfg = Config()
    cfg.token = "fake.test.token"  # a bot token, in the user field
    app = appmod.ScannerApp(cfg)

    async with app.run_test(size=(140, 40)) as pilot:
        for _ in range(60):
            await pilot.pause(0.1)
            if app.sources:
                break
        assert app.is_bot, "the app took Discord's word for it and still ran as a user"
        ok("Discord says the token is a bot, so it runs as one and says so")


async def main():
    test_user_token_wins()
    print()
    test_http_identifies_correctly()
    print()
    await test_user_only_routes_are_refused()
    print()
    await test_member_pagination()
    print()
    await test_identify_declares_intents()
    await test_missing_intent_is_explained()
    print()
    await test_bot_never_uses_the_sidebar()
    print()
    await test_bot_does_not_need_permissions_to_chunk()
    print()
    await test_app_runs_as_a_bot()
    await test_a_bot_token_in_the_user_field_is_corrected()
    print("\nALL BOT TOKEN TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
