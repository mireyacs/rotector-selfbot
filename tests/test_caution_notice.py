"""Acting on CAUTION findings, and telling people before you act.

Two rules are the point of these tests.

* A CAUTION finding is Rotector saying it found something and deliberately
  did not conclude from it. Acting is permitted, because the operator may
  reasonably decide -- but never by the same click that would action a
  Confirmed one. It takes a second, separate confirmation.
* The notice goes out *before* the action, because a banned account shares no
  server with you and cannot be DMed afterwards. And it is sent only by a bot:
  a user account messaging strangers in bulk is what Discord's spam heuristics
  are built to catch.

Discord is stubbed. Nothing is kicked, banned or messaged.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import rsb.tui.app as appmod
from rsb.config import Config
from rsb.discord.gateway import GuildMember
from rsb.discord.http import DiscordForbidden, Guild
from rsb.moderation import DEFAULT_NOTICE, build_notice, check_eligibility, plan_bulk
from rsb.rotector import MemberReport, RobloxAccount
from rsb.tui.app import Row
from rsb.tui.dialogs import BulkActionDialog, ModerationDialog
from rsb.verdict import Verdict
from textual.widgets import Button, Checkbox, DataTable, Input, RadioButton

ok = lambda m: print(f"[ok] {m}")

#: 2 Confirmed (THREAT), 4 Provisional and 5 Mixed (CAUTION), 0 Unflagged
LAYOUT = [(1, 2), (2, 4), (3, 5), (4, 0)]


def report(discord_id: str, flag: int | None) -> MemberReport:
    rep = MemberReport(discord_id=discord_id)
    if flag is not None:
        rep.accounts.append(
            RobloxAccount(
                user_id=int(discord_id), username=f"rblx{discord_id}",
                flag_type=flag, category=5,
                reasons={"Inappropriate content": {"message": "x",
                                                   "confidence": 0.6}},
            )
        )
    return rep


def make_row(n: int, flag: int | None) -> Row:
    return Row(
        member=GuildMember(id=str(n), username=f"user{n}", nick=f"User {n}"),
        report=report(str(n), flag),
    )


# --------------------------------------------------------------------------
# eligibility
# --------------------------------------------------------------------------


def test_caution_is_permitted_but_never_routine():
    confirmed = check_eligibility(report("1", 2))
    assert confirmed.allowed and not confirmed.needs_double_confirm
    ok("a Confirmed finding acts on one confirmation, as before")

    for flag, name in [(4, "Provisional"), (5, "Mixed")]:
        caution = check_eligibility(report("2", flag))
        assert caution.allowed, f"{name} was blocked outright"
        assert caution.needs_double_confirm, f"{name} passed without a second check"
        assert caution.needs_override
        ok(f"{name} (CAUTION) is allowed, and demands a second confirmation")

    off = check_eligibility(report("2", 4), allow_caution=False)
    assert not off.allowed
    ok("moderation.allow_caution = false blocks it again")

    for flag in (0, 3, None):
        other = check_eligibility(report("4", flag))
        assert not other.allowed
        assert not other.needs_double_confirm
    ok("everything weaker than CAUTION stays blocked by require_threat - "
       "the second confirmation is not a way around that gate")


def test_bulk_marks_which_targets_are_cautious():
    rows = [make_row(n, flag) for n, flag in LAYOUT]
    plan = plan_bulk(rows, "ban", past="banned")

    assert [t.member_id for t in plan.allowed] == ["1", "2", "3"]
    cautious = [t for t in plan.allowed if t.eligibility.needs_double_confirm]
    assert {t.member_id for t in cautious} == {"2", "3"}
    ok(f"a bulk plan knows which {len(cautious)} of its {len(plan.allowed)} "
       f"targets are CAUTION rather than THREAT")

    strict = plan_bulk(rows, "ban", past="banned", allow_caution=False)
    assert [t.member_id for t in strict.allowed] == ["1"]
    ok("and with allow_caution off they drop out of the plan entirely")


# --------------------------------------------------------------------------
# the notice
# --------------------------------------------------------------------------


def test_notice_says_what_it_should():
    text = build_notice(report("1", 2), "banned", "Cool Server")
    assert "banned" in text and "Cool Server" in text
    assert "rotector.com" in text.lower()
    ok("the notice names the action, the place, and where to appeal")

    assert "Confirmed" in text and "rblx1" in text
    ok("and the actual finding, rather than a bare accusation")

    stray = build_notice(report("1", 2), "kicked", "Srv", "Goodbye {typo}")
    assert "rotector.com" in stray.lower()
    ok("a template with stray braces still carries the appeal link")

    long = build_notice(report("1", 2), "banned", "S", "x" * 5000)
    assert len(long) <= 2000
    ok(f"and it is trimmed to Discord's message limit ({len(long)} chars)")


# --------------------------------------------------------------------------
# the app
# --------------------------------------------------------------------------


class FakeHTTP:
    def __init__(self, token, bot=False, **kw):
        self.is_bot = bot
        self.dms: list[tuple[str, str]] = []
        self.banned: list[str] = []
        self.kicked: list[str] = []
        #: ids whose DMs are closed
        self.refuse_dm: set[str] = set()
        #: order of operations, to prove the notice precedes the action
        self.order: list[str] = []

    async def me(self):
        return {"username": "t", "global_name": "T", "id": "9",
                "bot": self.is_bot}

    async def guilds(self):
        return [Guild(id="111", name="Cool Server", owner=True,
                      permissions=8, member_count=4, presence_count=2)]

    async def relationships(self):
        return []

    async def private_channels(self):
        return []

    async def widget(self, gid):
        return None

    async def channels(self, gid):
        from rsb.discord.http import Channel
        return [Channel(id="c1", name="general", type=0, position=0,
                        everyone_can_view=True)]

    async def send_dm(self, user_id, content):
        if user_id in self.refuse_dm:
            raise DiscordForbidden("cannot send messages to this user")
        self.order.append(f"dm:{user_id}")
        self.dms.append((user_id, content))
        return {"id": "m1"}

    async def ban(self, guild_id, user_id, reason, delete_message_seconds=0):
        self.order.append(f"ban:{user_id}")
        self.banned.append(user_id)

    async def kick(self, guild_id, user_id, reason):
        self.order.append(f"kick:{user_id}")
        self.kicked.append(user_id)

    async def aclose(self):
        pass


class FakeGateway:
    def __init__(self, token, bot=False):
        self.user = None
        self.is_bot = bot
        self.on_reconnect = None

    async def connect(self, timeout=45.0):
        return {}

    async def close(self):
        pass


async def wait_for(check, pilot, what, tries=80):
    for _ in range(tries):
        await pilot.pause(0.1)
        if check():
            return
    raise AssertionError(f"timed out waiting for {what}")


def populate(app):
    app.rows = {str(n): make_row(n, flag) for n, flag in LAYOUT}
    app.selected.clear()
    app._rebuild_table()


async def open_app(as_bot: bool):
    appmod.DiscordHTTP = FakeHTTP
    appmod.DiscordGateway = FakeGateway
    cfg = Config()
    if as_bot:
        cfg.bot_token = "fake.test.token"
    else:
        cfg.token = "fake.test.token"
    cfg.moderation.bulk_delay = 0.0
    return appmod.ScannerApp(cfg)


async def test_caution_needs_the_word_typed(app, pilot):
    """The second confirmation must not be satisfiable by the first click."""
    populate(app)
    app.current_source = next(s for s in app.sources if s.kind == "guild")
    table = app.query_one("#results", DataTable)

    # row 1 is the Provisional one under the default filter ordering
    order = [r.member.id for r in app._matching_rows()]
    table.move_cursor(row=order.index("2"))
    await pilot.pause(0.2)

    await pilot.press("b")
    await wait_for(lambda: isinstance(app.screen, ModerationDialog), pilot,
                   "the ban dialog")
    dialog = app.screen
    assert dialog.eligibility.needs_double_confirm
    ok("a CAUTION member opens the dialog in double-confirm mode")

    dialog.query_one("#confirm", Button).press()
    await pilot.pause(0.3)
    assert isinstance(app.screen, ModerationDialog)
    assert not app.http.banned
    ok("pressing Ban straight away does nothing")

    dialog.query_one("#override", Checkbox).value = True
    dialog.query_one("#confirm", Button).press()
    await pilot.pause(0.3)
    assert isinstance(app.screen, ModerationDialog)
    assert not app.http.banned
    ok("ticking the acknowledgement alone is still not enough")

    dialog.query_one("#double-confirm", Input).value = "ban"
    dialog.query_one("#confirm", Button).press()
    await wait_for(lambda: app.http.banned, pilot, "the ban")
    assert app.http.banned == ["2"], app.http.banned
    ok("typing the word as well is what actually confirms it")


async def test_threat_still_takes_one_confirmation(app, pilot):
    populate(app)
    app.current_source = next(s for s in app.sources if s.kind == "guild")
    app.http.banned.clear()
    table = app.query_one("#results", DataTable)
    order = [r.member.id for r in app._matching_rows()]
    table.move_cursor(row=order.index("1"))
    await pilot.pause(0.2)

    await pilot.press("b")
    await wait_for(lambda: isinstance(app.screen, ModerationDialog), pilot,
                   "the ban dialog")
    dialog = app.screen
    assert not dialog.eligibility.needs_double_confirm
    try:
        dialog.query_one("#double-confirm", Input)
        raise AssertionError("a Confirmed finding was made harder to action")
    except Exception as exc:
        if isinstance(exc, AssertionError):
            raise
    dialog.query_one("#confirm", Button).press()
    await wait_for(lambda: app.http.banned, pilot, "the ban")
    ok("a THREAT finding is unchanged: one confirmation, no extra typing")


async def test_bot_notifies_before_acting(app, pilot):
    populate(app)
    app.current_source = next(s for s in app.sources if s.kind == "guild")
    app.http.banned.clear()
    app.http.order.clear()
    table = app.query_one("#results", DataTable)
    order = [r.member.id for r in app._matching_rows()]
    table.move_cursor(row=order.index("1"))
    await pilot.pause(0.2)

    await pilot.press("b")
    await wait_for(lambda: isinstance(app.screen, ModerationDialog), pilot,
                   "the ban dialog")
    dialog = app.screen
    assert dialog.can_notify, "a bot was not offered the notice"
    assert dialog.query_one("#notify", Checkbox).value is True
    ok("as a bot, the dialog offers to DM them first and defaults to yes")

    dialog.query_one("#confirm", Button).press()
    await wait_for(lambda: app.http.banned, pilot, "the ban")
    await pilot.pause(0.3)

    assert app.http.order == ["dm:1", "ban:1"], app.http.order
    ok(f"the DM went out BEFORE the ban: {app.http.order}")

    _, body = app.http.dms[0]
    assert "rotector.com" in body.lower() and "Cool Server" in body
    ok("and it names the server and the appeal route")


async def test_closed_dms_do_not_stop_the_action(app, pilot):
    populate(app)
    app.current_source = next(s for s in app.sources if s.kind == "guild")
    app.http.banned.clear()
    app.http.order.clear()
    app.http.dms.clear()
    app.http.refuse_dm = {"1"}

    table = app.query_one("#results", DataTable)
    order = [r.member.id for r in app._matching_rows()]
    table.move_cursor(row=order.index("1"))
    await pilot.pause(0.2)
    await pilot.press("b")
    await wait_for(lambda: isinstance(app.screen, ModerationDialog), pilot,
                   "the ban dialog")
    app.screen.query_one("#confirm", Button).press()
    await wait_for(lambda: app.http.banned, pilot, "the ban")

    assert app.http.banned == ["1"]
    assert not app.http.dms
    ok("someone with DMs closed is still actioned - a refused notice is "
       "the normal case, not a failure")
    app.http.refuse_dm = set()


async def test_bulk_notifies_and_gates_caution(app, pilot):
    from rsb.tui.dialogs import BulkChoice

    populate(app)
    app.current_source = next(s for s in app.sources if s.kind == "guild")
    app.http.banned.clear()
    app.http.order.clear()
    app.http.dms.clear()

    plan = plan_bulk(list(app.rows.values()), "ban", past="banned")
    app.run_bulk("ban", plan, BulkChoice(scope="filtered", notify=True))
    await wait_for(lambda: len(app.http.banned) >= 3, pilot, "the bans")
    await pilot.pause(0.4)

    assert app.http.order == [
        "dm:1", "ban:1", "dm:2", "ban:2", "dm:3", "ban:3"
    ], app.http.order
    ok(f"in bulk each member is told first, then actioned: {app.http.order}")
    assert "4" not in app.http.banned
    ok("and the Unflagged member is untouched throughout")


async def test_bulk_dialog_refuses_caution_without_acknowledgement(app, pilot):
    populate(app)
    rows = list(app.rows.values())
    result: list = []

    def resolve(scope, custom=None):
        from rsb.moderation import rows_for_scope
        return plan_bulk(rows_for_scope(scope, [], rows), "ban", past="banned",
                         custom=custom)

    from rsb.moderation import BULK_SCOPES
    app.push_screen(
        BulkActionDialog(action="ban", resolve=resolve, scopes=BULK_SCOPES,
                         has_selection=False, can_notify=True),
        result.append,
    )
    await wait_for(lambda: isinstance(app.screen, BulkActionDialog), pilot,
                   "the bulk dialog")
    dialog = app.screen
    dialog.query_one("#scope-filtered", RadioButton).value = True
    await pilot.pause(0.3)

    assert dialog.query_one("#caution-ack", Checkbox).display
    ok("a plan containing CAUTION members shows its own acknowledgement")

    dialog.query_one("#confirm-count", Input).value = str(len(dialog.plan.allowed))
    dialog.query_one("#confirm", Button).press()
    await pilot.pause(0.3)
    assert not result, "the typed count alone let CAUTION members through"
    ok("the typed count alone does not get them through")

    dialog.query_one("#caution-ack", Checkbox).value = True
    dialog.query_one("#confirm", Button).press()
    await pilot.pause(0.4)
    assert result and result[0] is not None
    ok("both the acknowledgement and the count are required")

    # and a THREAT-only scope should not ask for it at all
    app.push_screen(
        BulkActionDialog(action="ban", resolve=resolve, scopes=BULK_SCOPES,
                         has_selection=False, can_notify=True),
        lambda r: None,
    )
    await wait_for(lambda: isinstance(app.screen, BulkActionDialog), pilot,
                   "the second dialog")
    second = app.screen
    second.query_one("#scope-threat", RadioButton).value = True
    await pilot.pause(0.3)
    assert not second.query_one("#caution-ack", Checkbox).display
    ok("a THREAT-only scope is not asked to acknowledge anything extra")
    await pilot.press("escape")
    await pilot.pause(0.3)


async def test_user_token_never_dms(app, pilot):
    """The whole point of the restriction: this is a user account."""
    from rsb.tui.dialogs import BulkChoice

    populate(app)
    app.current_source = next(s for s in app.sources if s.kind == "guild")
    assert not app.is_bot

    table = app.query_one("#results", DataTable)
    order = [r.member.id for r in app._matching_rows()]
    app.http.dms.clear()
    app.http.banned.clear()
    table.move_cursor(row=order.index("1"))
    await pilot.pause(0.2)
    await pilot.press("b")
    await wait_for(lambda: isinstance(app.screen, ModerationDialog), pilot,
                   "the ban dialog")
    dialog = app.screen
    assert not dialog.can_notify
    try:
        dialog.query_one("#notify", Checkbox)
        raise AssertionError("a user account was offered the DM option")
    except Exception as exc:
        if isinstance(exc, AssertionError):
            raise
    ok("a user token is not offered the notice at all")

    dialog.query_one("#confirm", Button).press()
    await wait_for(lambda: app.http.banned, pilot, "the ban")
    assert not app.http.dms
    ok("and none is sent")

    # even if something hands the worker a choice that asks for one
    app.http.order.clear()
    app.rows["1"].actioned = None
    plan = plan_bulk([app.rows["1"]], "ban", past="banned")
    app.run_bulk("ban", plan, BulkChoice(scope="threat", notify=True))
    await wait_for(lambda: len(app.http.order) >= 1, pilot, "the bulk ban")
    await pilot.pause(0.3)
    assert not app.http.dms, app.http.dms
    assert app.http.order == ["ban:1"], app.http.order
    ok("and a choice asking for one anyway is still refused by the worker")


async def main():
    test_caution_is_permitted_but_never_routine()
    print()
    test_bulk_marks_which_targets_are_cautious()
    print()
    test_notice_says_what_it_should()
    print()

    bot_app = await open_app(as_bot=True)
    async with bot_app.run_test(size=(150, 45)) as pilot:
        await wait_for(lambda: bot_app.sources, pilot, "startup")
        await test_caution_needs_the_word_typed(bot_app, pilot)
        print()
        await test_threat_still_takes_one_confirmation(bot_app, pilot)
        print()
        await test_bot_notifies_before_acting(bot_app, pilot)
        print()
        await test_closed_dms_do_not_stop_the_action(bot_app, pilot)
        print()
        await test_bulk_notifies_and_gates_caution(bot_app, pilot)
        print()
        await test_bulk_dialog_refuses_caution_without_acknowledgement(
            bot_app, pilot
        )
        print()

    user_app = await open_app(as_bot=False)
    async with user_app.run_test(size=(150, 45)) as pilot:
        await wait_for(lambda: user_app.sources, pilot, "startup")
        await test_user_token_never_dms(user_app, pilot)

    print("\nALL CAUTION / NOTICE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
