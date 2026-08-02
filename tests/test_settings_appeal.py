"""Editing credentials in the settings screen, and your own appeal route.

The first half is a bug this file exists to keep fixed: swapping the user
token for a bot token in the settings screen wrote the file correctly and then
did nothing, because the app never signed in again. "Restart to apply" is
indistinguishable from "did nothing" when the thing you changed is who you are.

The second half is the appeal gateway. Rotector's terms require that anyone
actioned on their data can appeal *to Rotector*, so a route of your own is
always offered in addition -- never in place of it.

Nothing here touches the network.
"""
import asyncio
import sys
import tempfile
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import rsb.tui.app as appmod
from rsb.config import AppealConfig, Config
from rsb.discord.http import MAX_REASON, Guild
from rsb.migrate import SCHEMA, migrate_config
from rsb.moderation import appeal_lines, build_notice, build_reason
from rsb.rotector import MemberReport, RobloxAccount
from rsb.tui.settings import SettingsScreen, _widget_id
from textual.widgets import Button, Input

ok = lambda m: print(f"[ok] {m}")


def report(flag: int = 2) -> MemberReport:
    rep = MemberReport(discord_id="1")
    rep.accounts.append(
        RobloxAccount(user_id=7, username="rblx7", flag_type=flag, category=5)
    )
    return rep


# --------------------------------------------------------------------------
# every setting actually loads
# --------------------------------------------------------------------------


def test_every_setting_round_trips():
    """A setting in the schema that the loader ignores is a silent lie."""
    import dataclasses

    workdir = Path(tempfile.mkdtemp())
    path = workdir / "config.toml"
    path.write_text("")
    migrate_config(path)

    cfg = Config()
    cfg._apply_file(path)

    checked = 0
    for section in SCHEMA:
        if section.name == "discord":
            continue
        holder = getattr(cfg, section.name, None)
        if holder is None or not dataclasses.is_dataclass(holder):
            continue
        names = {f.name for f in dataclasses.fields(holder)}
        for setting in section.settings:
            assert setting.name in names, (
                f"{section.name}.{setting.name} is in the schema but not on "
                f"the config object"
            )
            checked += 1
    ok(f"all {checked} non-credential settings exist on the config object")

    # and the values in the file are the ones that come back out
    path.write_text(
        "[moderation]\n"
        "bulk_delay = 4.5\n"
        "allow_caution = false\n"
        "notify_before_action = false\n"
        "[appeal]\n"
        'invite = "https://discord.gg/appeals"\n'
        "include_in_reason = true\n"
    )
    fresh = Config()
    fresh._apply_file(path)
    assert fresh.moderation.bulk_delay == 4.5, fresh.moderation.bulk_delay
    assert fresh.moderation.allow_caution is False
    assert fresh.moderation.notify_before_action is False
    assert fresh.appeal.invite == "https://discord.gg/appeals"
    assert fresh.appeal.include_in_reason is True
    ok("and values written to the file are the values that load back")


# --------------------------------------------------------------------------
# the appeal route
# --------------------------------------------------------------------------


def test_appeal_is_additional_never_instead():
    full = AppealConfig(
        invite="https://discord.gg/appeals",
        contact="mods@example.com",
        note="Include your Roblox username.",
        include_in_reason=True,
    )

    notice = build_notice(report(), "banned", "Cool Server", appeal=full)
    assert "rotector.com" in notice.lower(), notice
    ok("the notice still carries Rotector's appeal link, as their terms require")
    assert "discord.gg/appeals" in notice and "mods@example.com" in notice
    assert "Include your Roblox username." in notice
    ok("and your own route is added underneath it")

    reason = build_reason(report(), appeal=full)
    assert "rotector.com" in reason.lower() and "discord.gg/appeals" in reason
    ok(f"the audit-log reason carries both: {reason[-60:]}")

    off = build_reason(report(), appeal=AppealConfig(invite="https://x.example"))
    assert "x.example" not in off
    ok("include_in_reason = false keeps it out of the audit log")

    none = build_reason(report(), appeal=AppealConfig())
    assert "rotector.com" in none.lower()
    assert appeal_lines(AppealConfig()) == []
    assert appeal_lines(None) == []
    ok("an unconfigured appeal section changes nothing at all")


def test_appeal_never_displaces_the_required_link():
    """The optional route must never crowd out the mandatory one."""
    huge = AppealConfig(invite="https://discord.gg/" + "x" * 600,
                        include_in_reason=True)
    reason = build_reason(report(), appeal=huge)
    assert len(reason) <= MAX_REASON, len(reason)
    assert "rotector.com" in reason.lower(), reason
    ok(f"an over-long invite is dropped rather than trimming Rotector's link "
       f"({len(reason)} chars, limit {MAX_REASON})")

    long_note = AppealConfig(note="y" * 4000)
    notice = build_notice(report(), "banned", "S", appeal=long_note)
    assert len(notice) <= 2000
    assert "rotector.com" in notice.lower()
    ok("same for the notice against Discord's 2000-character message limit")


# --------------------------------------------------------------------------
# swapping credentials in the settings screen
# --------------------------------------------------------------------------


class FakeHTTP:
    #: every client built during the test, so the swap can be observed
    made: list = []

    def __init__(self, token, bot=False, **kw):
        self.token = token
        self.is_bot = bot
        FakeHTTP.made.append(self)

    async def me(self):
        return {"username": "bot" if self.is_bot else "user",
                "global_name": None, "id": "9", "bot": self.is_bot}

    async def guilds(self):
        return [Guild(id="111", name="Srv", owner=True, permissions=8,
                      member_count=3, presence_count=1)]

    async def relationships(self):
        return []

    async def private_channels(self):
        return []

    async def widget(self, gid):
        return None

    async def aclose(self):
        pass


class FakeGateway:
    def __init__(self, token, bot=False):
        self.token = token
        self.is_bot = bot
        self.user = None
        self.on_reconnect = None

    async def connect(self, timeout=45.0):
        return {}

    async def close(self):
        pass


async def wait_for(check, pilot, what, tries=100):
    for _ in range(tries):
        await pilot.pause(0.1)
        if check():
            return
    raise AssertionError(f"timed out waiting for {what}")


async def test_swapping_to_a_bot_token_applies_immediately():
    appmod.DiscordHTTP = FakeHTTP
    appmod.DiscordGateway = FakeGateway
    FakeHTTP.made.clear()

    workdir = Path(tempfile.mkdtemp())
    path = workdir / "config.toml"
    path.write_text('[discord]\ntoken = "user.tok.en"\n')
    cfg = Config()
    cfg._apply_file(path)
    cfg.source = path

    app = appmod.ScannerApp(cfg)
    async with app.run_test(size=(150, 45)) as pilot:
        await wait_for(lambda: app.sources, pilot, "the first connection")
        assert not app.is_bot
        ok("started as a user account")

        await pilot.press("ctrl+s")
        await wait_for(lambda: isinstance(app.screen, SettingsScreen), pilot,
                       "the settings screen")
        screen = app.screen

        # exactly what the report described: clear the token, fill in the bot one
        screen.query_one(f"#{_widget_id('discord', 'token')}", Input).value = ""
        screen.query_one(
            f"#{_widget_id('discord', 'bot_token')}", Input
        ).value = "bot.tok.en"
        screen.query_one("#save", Button).press()
        await wait_for(lambda: not isinstance(app.screen, SettingsScreen), pilot,
                       "the screen to close")

        saved = tomllib.loads(path.read_text())
        assert saved["discord"]["token"] == ""
        assert saved["discord"]["bot_token"] == "bot.tok.en"
        ok("the file is written: token cleared, bot_token set")

        assert app.config.is_bot, "the live config still thinks it is a user"
        ok("the running config switches to bot mode")

        await wait_for(lambda: app.is_bot, pilot, "the app to sign in again")
        ok("and the app signs in again by itself, without a restart")

        assert app.http.token == "bot.tok.en", app.http.token
        assert app.http.is_bot
        ok(f"the live client is now the bot one ({len(FakeHTTP.made)} built "
           f"in total, the newest being the bot)")

        await wait_for(lambda: app.sources, pilot, "the sources to reload")
        assert {s.kind for s in app.sources} == {"guild"}
        ok("sources reloaded under the new identity: servers only, as a bot")


async def test_environment_override_is_reported():
    """An env token silently beats the file. Saying so beats appearing broken."""
    appmod.DiscordHTTP = FakeHTTP
    appmod.DiscordGateway = FakeGateway

    workdir = Path(tempfile.mkdtemp())
    path = workdir / "config.toml"
    path.write_text('[discord]\ntoken = "user.tok.en"\n')
    cfg = Config()
    cfg._apply_file(path)
    cfg.source = path
    cfg.token_from_env = True

    assert cfg.env_overrides() == ["DISCORD_TOKEN"]

    app = appmod.ScannerApp(cfg)
    async with app.run_test(size=(150, 45)) as pilot:
        await wait_for(lambda: app.sources, pilot, "the connection")
        app.config.bot_token = "bot.tok.en"
        app.config.token = None
        app.reauthenticate()
        await pilot.pause(0.8)

        status = app._status_text
        assert "DISCORD_TOKEN" in status, status
        ok(f"it names the variable rather than failing quietly: "
           f"{status[:70]}...")


async def main():
    test_every_setting_round_trips()
    print()
    test_appeal_is_additional_never_instead()
    print()
    test_appeal_never_displaces_the_required_link()
    print()
    await test_swapping_to_a_bot_token_applies_immediately()
    print()
    await test_environment_override_is_reported()
    print("\nALL SETTINGS / APPEAL TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
