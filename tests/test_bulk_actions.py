"""Selecting many members, and acting on them at once.

Bulk moderation is the least recoverable thing this program does, so most of
what is tested here is refusal: that ineligible members are excluded rather
than swept along, that the confirmation cannot be clicked past, that one
failure does not strand the rest of the run, and that stop means stop.

Discord is stubbed throughout -- nothing is really kicked or banned.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import rsb.tui.app as appmod
from rsb.config import Config
from rsb.discord.gateway import GuildMember
from rsb.discord.http import DiscordForbidden, Guild
from rsb.moderation import BULK_SCOPES, plan_bulk, rows_for_scope
from rsb.rotector import MemberReport, RobloxAccount
from rsb.tui.app import PAGE_SIZE, Row
from rsb.tui.dialogs import BulkActionDialog, BulkPickDialog
from rsb.verdict import Verdict
from textual.widgets import Button, DataTable, Input, RadioButton

ok = lambda m: print(f"[ok] {m}")


def report(discord_id: str, flag: int | None, category: int = 5) -> MemberReport:
    rep = MemberReport(discord_id=discord_id)
    if flag is not None:
        rep.accounts.append(
            RobloxAccount(
                user_id=int(discord_id),
                username=f"rblx{discord_id}",
                flag_type=flag,
                category=category,
                reasons={"Inappropriate content": {"message": "test fixture",
                                                  "confidence": 0.9}},
            )
        )
    return rep


def make_row(n: int, flag: int | None) -> Row:
    return Row(
        member=GuildMember(id=str(n), username=f"user{n}", nick=f"User {n}"),
        report=report(str(n), flag),
    )


#: 2 = Confirmed and 1 = Flagged are actionable; 0/5 are not
LAYOUT = [(1, 2), (2, 2), (3, 1), (4, 0), (5, 5), (6, None)]


class FakeHTTP:
    def __init__(self, token, **kw):
        self.kicked: list[tuple[str, str]] = []
        self.banned: list[tuple[str, str]] = []
        self.blocked: list[str] = []
        self.unfriended: list[str] = []
        #: member ids to refuse, to prove one failure does not end a run
        self.refuse: set[str] = set()
        self.calls = 0

    async def me(self):
        return {"username": "t", "global_name": "T", "id": "9"}

    async def guilds(self):
        return [Guild(id="111", name="Srv", owner=True, permissions=8,
                      member_count=len(LAYOUT), presence_count=2)]

    async def relationships(self):
        return []

    async def widget(self, gid):
        return None

    async def private_channels(self):
        return []

    async def channels(self, gid):
        from rsb.discord.http import Channel
        return [Channel(id="c1", name="general", type=0, position=0,
                        everyone_can_view=True)]

    def _check(self, user_id):
        self.calls += 1
        if user_id in self.refuse:
            raise DiscordForbidden("missing permissions")

    async def kick(self, guild_id, user_id, reason):
        self._check(user_id)
        self.kicked.append((user_id, reason))

    async def ban(self, guild_id, user_id, reason, delete_message_seconds=0):
        self._check(user_id)
        self.banned.append((user_id, reason))

    async def block_user(self, user_id):
        self._check(user_id)
        self.blocked.append(user_id)

    async def remove_friend(self, user_id):
        self._check(user_id)
        self.unfriended.append(user_id)

    async def aclose(self):
        pass


class FakeGateway:
    def __init__(self, token, bot=False):
        self.user = None
        self.on_reconnect = None

    async def connect(self, timeout=45.0):
        return {}

    async def close(self):
        pass


def populate(app, layout=LAYOUT):
    app.rows = {str(n): make_row(n, flag) for n, flag in layout}
    app.selected.clear()
    app._rebuild_table()


def source_of(app, kind="guild"):
    return next(s for s in app.sources if s.kind == kind)


# --------------------------------------------------------------------------
# planning
# --------------------------------------------------------------------------


def test_planning():
    rows = [make_row(n, flag) for n, flag in LAYOUT]

    plan = plan_bulk(rows, "ban", require_threat=True)
    assert [t.member_id for t in plan.allowed] == ["1", "2", "3"], plan.allowed
    assert [t.member_id for t in plan.blocked] == ["4", "5", "6"]
    ok(f"gated: {plan.describe()}")

    for target in plan.blocked:
        assert not target.eligibility.allowed
        assert target.eligibility.explanation
    ok("every excluded member carries the reason it was excluded")

    for target in plan.allowed:
        assert "rotector.com" in target.reason.lower(), target.reason
    ok("every reason carries the appeal link, as Rotector's terms require")

    ungated = plan_bulk(rows, "block", gated=False)
    assert len(ungated.allowed) == len(rows) and not ungated.blocked
    ok("blocking is not gated on a finding -- your boundaries are your own")

    override = plan_bulk(rows, "ban", require_threat=False)
    assert len(override.allowed) == len(rows)
    ok("require_threat=False lets the rest through, as configured")

    threats = rows_for_scope("threat", [], rows)
    assert {r.member.id for r in threats} == {"1", "2", "3"}
    caution = rows_for_scope("caution", [], rows)
    assert {r.member.id for r in caution} == {"1", "2", "3", "5"}
    picked = rows_for_scope("selected", [rows[0]], rows)
    assert [r.member.id for r in picked] == ["1"]
    assert len(rows_for_scope("filtered", [], rows)) == len(rows)
    ok(f"all {len(BULK_SCOPES)} scopes resolve to the right members")


# --------------------------------------------------------------------------
# selection
# --------------------------------------------------------------------------


async def test_selection(app, pilot):
    populate(app)
    table = app.query_one("#results", DataTable)

    table.move_cursor(row=0)
    await pilot.press("space")
    await pilot.pause(0.1)
    assert app.selected == {"1"}, app.selected
    assert "✓" in str(app._name_cell(app.rows["1"]))
    ok("space ticks the highlighted member and the row shows it")

    await pilot.press("space")
    await pilot.pause(0.1)
    assert app.selected == set()
    ok("pressing it again unticks")

    for index in (0, 2):
        table.move_cursor(row=index)
        await pilot.press("space")
    await pilot.pause(0.1)
    assert app.selected == {"1", "3"}
    ok(f"several can be ticked at once: {sorted(app.selected)}")

    from rsb.tui.app import FilterMode

    app.filter_mode = FilterMode.THREATS
    app._rebuild_table()
    await pilot.pause(0.1)
    assert app.selected == {"1", "3"}, "a filter change dropped the selection"
    ok("changing the filter does not silently lose what was selected")

    await pilot.press("X")
    await pilot.pause(0.1)
    assert not app.selected
    ok("X clears it")

    # a narrow filter first: 'all shown' must mean shown, not everyone held
    await pilot.press("A")
    await pilot.pause(0.1)
    assert app.selected == {"1", "2", "3"}, app.selected
    ok(f"A under 'Threats only' selected {len(app.selected)}, "
       f"not all {len(LAYOUT)} held")

    await pilot.press("A")
    await pilot.pause(0.1)
    assert not app.selected
    ok("and a second A unselects them again")

    app.filter_mode = FilterMode.ALL
    app._rebuild_table()
    await pilot.press("A")
    await pilot.pause(0.1)
    assert app.selected == {str(n) for n, _ in LAYOUT}
    ok(f"under 'Everything' it selects all {len(app.selected)}")
    await pilot.press("X")
    await pilot.pause(0.1)


async def test_space_still_types(app, pilot):
    """Binding space must not make it impossible to type a space."""
    populate(app)
    app.selected.clear()
    await pilot.press("slash")
    await pilot.pause(0.3)
    search = app.query_one("#search", Input)
    search.focus()
    await pilot.pause(0.2)
    await pilot.press("u", "s", "e", "space", "r")
    await pilot.pause(0.3)

    assert " " in search.value, f"space was swallowed: {search.value!r}"
    ok(f"a space typed into the search box is a space: {search.value!r}")
    assert not app.selected, "typing in a text box selected a member"
    ok("and it did not tick anyone while typing")

    await pilot.press("escape")
    await pilot.pause(0.3)
    populate(app)


async def test_select_all_spans_pages(app, pilot):
    """Selecting 'all' must not quietly mean 'this page'."""
    from rsb.tui.app import FilterMode

    big = [(n, 2 if n % 2 else 1) for n in range(1, PAGE_SIZE * 2 + 41)]
    app.filter_mode = FilterMode.ALL
    populate(app, big)
    await pilot.pause(0.1)
    assert app.page_count >= 3, app.page_count

    shown = len(app._shown)
    await pilot.press("A")
    await pilot.pause(0.2)
    assert len(app.selected) == len(big), (len(app.selected), len(big))
    ok(f"A selected all {len(app.selected):,} across {app.page_count} pages, "
       f"not just the {shown} on screen")

    rows = app._selected_rows()
    assert len(rows) == len(big)
    ok("and every one of them resolves back to a row for the plan")

    app.selected.clear()
    populate(app)


# --------------------------------------------------------------------------
# the confirmation
# --------------------------------------------------------------------------


async def test_confirmation_cannot_be_clicked_past(app, pilot):
    populate(app)
    rows = list(app.rows.values())

    def resolve(scope, custom=None):
        return plan_bulk(
            rows_for_scope(scope, [], rows), "ban", require_threat=True,
            custom=custom,
        )

    result: list = []

    def done(value):
        result.append(value)

    app.push_screen(
        BulkActionDialog(
            action="ban", resolve=resolve, scopes=BULK_SCOPES,
            has_selection=False, wants_purge=True,
        ),
        done,
    )
    await pilot.pause(0.3)
    dialog = app.screen
    assert isinstance(dialog, BulkActionDialog)

    assert dialog.query_one("#scope-selected", RadioButton).disabled
    ok("with nothing selected, the 'selected' scope is not offered at all")

    dialog.query_one("#confirm", Button).press()
    await pilot.pause(0.3)
    assert app.screen is dialog and not result
    ok("pressing the action with an empty confirmation does nothing")

    dialog.query_one("#confirm-count", Input).value = "99"
    dialog.query_one("#confirm", Button).press()
    await pilot.pause(0.3)
    assert app.screen is dialog and not result
    ok("a wrong count does not get through either")

    # aim it at everything the filter shows, so the split is visible
    dialog.query_one("#scope-filtered", RadioButton).value = True
    await pilot.pause(0.3)
    assert dialog.scope == "filtered", dialog.scope
    assert len(dialog.plan.blocked) == 3, dialog.plan.blocked
    ok(f"switching scope re-plans: {dialog.plan.describe()}")

    expected = len(dialog.plan.allowed)
    dialog.query_one("#confirm-count", Input).value = str(expected)
    dialog.query_one("#confirm", Button).press()
    await pilot.pause(0.4)
    assert result and result[0] is not None, result
    ok(f"only typing the exact number ({expected}) confirms it")

    plan = resolve(result[0].scope, result[0].custom)
    assert len(plan.allowed) == expected
    assert {t.member_id for t in plan.blocked} == {"4", "5", "6"}
    ok("and the confirmed plan still excludes the ineligible members")


# --------------------------------------------------------------------------
# running
# --------------------------------------------------------------------------


async def wait_for(check, pilot, what, tries=80):
    for _ in range(tries):
        await pilot.pause(0.1)
        if check():
            return
    raise AssertionError(f"timed out waiting for {what}")


async def test_run(app, pilot):
    from rsb.tui.dialogs import BulkChoice

    populate(app)
    app.current_source = source_of(app)
    app.config.moderation.bulk_delay = 0.0
    rows = list(app.rows.values())
    plan = plan_bulk(rows, "ban", require_threat=True)

    app.run_bulk("ban", plan, BulkChoice(scope="threat"))
    await wait_for(lambda: len(app.http.banned) >= 3, pilot, "the bans")
    await pilot.pause(0.4)

    assert {i for i, _ in app.http.banned} == {"1", "2", "3"}
    ok(f"banned exactly the eligible members: {[i for i, _ in app.http.banned]}")

    assert not any(i in {"4", "5", "6"} for i, _ in app.http.banned)
    ok("the ineligible three were never sent to Discord at all")

    for member_id in ("1", "2", "3"):
        assert app.rows[member_id].actioned == "banned"
    assert app.rows["4"].actioned is None
    ok("acted-on rows are marked, and the others left alone")
    assert not app.selected
    ok("and they drop out of the selection as they are done")


async def test_one_failure_does_not_strand_the_run(app, pilot):
    from rsb.tui.dialogs import BulkChoice

    populate(app)
    app.current_source = source_of(app)
    app.config.moderation.bulk_delay = 0.0
    app.http.kicked.clear()
    app.http.refuse = {"2"}

    plan = plan_bulk(list(app.rows.values()), "kick", require_threat=True)
    app.run_bulk("kick", plan, BulkChoice(scope="threat"))
    await wait_for(lambda: len(app.http.kicked) >= 2, pilot, "the kicks")
    await pilot.pause(0.4)

    assert {i for i, _ in app.http.kicked} == {"1", "3"}, app.http.kicked
    ok("Discord refusing one member did not stop the other two")
    assert app.rows["2"].actioned is None
    ok("and the refused member is not marked as though it worked")
    app.http.refuse = set()


async def test_stop_interrupts(app, pilot):
    from rsb.tui.dialogs import BulkChoice

    many = [(n, 2) for n in range(100, 140)]
    populate(app, many)
    app.current_source = source_of(app)
    app.config.moderation.bulk_delay = 0.05
    app.http.banned.clear()

    plan = plan_bulk(list(app.rows.values()), "ban", require_threat=True)
    assert len(plan.allowed) == len(many)

    app.run_bulk("ban", plan, BulkChoice(scope="filtered"))
    await wait_for(lambda: len(app.http.banned) >= 3, pilot, "the run to start")
    await pilot.press("x")
    await wait_for(lambda: not app._bulk_running, pilot, "the run to stop")

    stopped_at = len(app.http.banned)
    await pilot.pause(0.6)
    assert len(app.http.banned) == stopped_at, (
        f"kept going after stop ({stopped_at} -> {len(app.http.banned)})"
    )
    assert stopped_at < len(many), "stopped only once it had finished anyway"
    ok(f"stop halted the run at {stopped_at} of {len(many)}, and it stayed halted")


async def test_pacing(app, pilot):
    """A bulk run must not fire as fast as the event loop allows."""
    from rsb.tui.dialogs import BulkChoice

    populate(app, [(n, 2) for n in range(200, 205)])
    app.current_source = source_of(app)
    app.config.moderation.bulk_delay = 0.2
    app.http.banned.clear()

    plan = plan_bulk(list(app.rows.values()), "ban", require_threat=True)
    started = asyncio.get_running_loop().time()
    app.run_bulk("ban", plan, BulkChoice(scope="filtered"))
    await wait_for(lambda: len(app.http.banned) >= 5, pilot, "the run", tries=200)
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed >= 0.2 * 4 * 0.8, f"5 bans in {elapsed:.2f}s is not paced"
    ok(f"5 actions took {elapsed:.2f}s at a {0.2}s spacing, rather than a burst")
    app.config.moderation.bulk_delay = 0.0


async def test_pick_dialog_matches_the_source(app, pilot):
    populate(app)
    app.current_source = source_of(app)
    await pilot.press("B")
    await wait_for(lambda: isinstance(app.screen, BulkPickDialog), pilot,
                   "the bulk dialog")
    labels = [label for _, label in app.screen.choices]
    assert labels == ["kick", "ban"], labels
    ok(f"in a server the bulk options are {labels}")
    await pilot.press("escape")
    await pilot.pause(0.3)


async def main():
    appmod.DiscordHTTP = FakeHTTP
    appmod.DiscordGateway = FakeGateway

    test_planning()
    print()

    cfg = Config()
    cfg.token = "fake.test.token"
    app = appmod.ScannerApp(cfg)

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.8)
        await test_selection(app, pilot)
        print()
        await test_space_still_types(app, pilot)
        print()
        await test_select_all_spans_pages(app, pilot)
        print()
        await test_confirmation_cannot_be_clicked_past(app, pilot)
        print()
        await test_run(app, pilot)
        print()
        await test_one_failure_does_not_strand_the_run(app, pilot)
        await test_stop_interrupts(app, pilot)
        await test_pacing(app, pilot)
        await test_pick_dialog_matches_the_source(app, pilot)

    print("\nALL BULK ACTION TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
