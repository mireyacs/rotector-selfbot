"""The scan queue: ordering, pausing, and the budget everything shares.

The rule this leans on hardest is the shared budget. A limiter per job would
let three queued scans make three times the documented request rate, which is
the exact thing Rotector's terms prohibit -- so the pool hands every job on a
backend the same limiter, and the test below proves they are the same object
rather than merely configured alike.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rsb.jobs import BudgetPool, JobQueue, JobState, ScanJob  # noqa: E402
from rsb.ratelimit import RateLimiter  # noqa: E402
from rsb.sources import ScanSource  # noqa: E402

ok = lambda m: print(f"[ok] {m}")


def source(name="Roblox Trading Hub", ident="1", members=10833):
    return ScanSource(kind="guild", id=ident, name=name, member_count=members)


# --- a job knows what it is -----------------------------------------------
job = ScanJob(source=source(), backend="rotector")
assert job.state is JobState.PENDING
assert job.label == "Roblox Trading Hub · rotector"
assert "guild" in job.title or "server" in job.title.lower(), job.title
assert job.progress is None, "nothing found yet means no progress to report"
job.expected, job.found = 10833, 5416
assert 0.49 < job.progress < 0.51, job.progress
ok(f"a job names its source and backend: {job.label!r}")

try:
    ScanJob(source=source(), backend="rotektor")
except ValueError as exc:
    ok(f"an unknown backend is refused at construction: {exc}")
else:
    raise AssertionError("ScanJob accepted a backend that does not exist")


# --- the same source, twice, on different backends ------------------------
queue = JobQueue(max_concurrent=2)
one = queue.add(ScanJob(source=source(), backend="rotector"))
two = queue.add(ScanJob(source=source(), backend="okappiki"))
assert queue.duplicate_of("1", "rotector") is one
assert queue.duplicate_of("1", "okappiki") is two
assert one.id != two.id and one.label != two.label
assert queue.active_id == one.id, "the first job added becomes the visible one"
ok("one server can be queued twice, once per backend, and they stay distinct")

# the same pair twice is a mis-click, and is reported as one
again = queue.duplicate_of("1", "rotector")
assert again is one
# ...but a different source on a taken backend is not
assert queue.duplicate_of("2", "rotector") is None
ok("only the same source *and* backend counts as a duplicate")


# --- the budget is shared, not multiplied ---------------------------------
pool = BudgetPool()
rotector = RateLimiter(limit=50, window=10.0, reserve=5)
okappiki = RateLimiter(limit=5, window=1.0, reserve=0)
pool.register("rotector", rotector)
pool.register("okappiki", okappiki)

jobs = [ScanJob(source=source(ident=str(i)), backend="rotector") for i in range(3)]
limiters = {id(pool.limiter(j.backend)) for j in jobs}
assert len(limiters) == 1, "three jobs on one backend must share one limiter"
assert pool.limiter("rotector") is rotector
assert pool.limiter("okappiki") is not rotector
ok("every job on a backend draws on the same limiter object, not a copy of it")

for job in jobs:
    job.state = JobState.RUNNING
assert pool.sharing(jobs) == {"rotector": 3}
ok("the pool can say how many running scans are splitting each budget")


# --- scheduling -----------------------------------------------------------
queue = JobQueue(max_concurrent=2)
a = queue.add(ScanJob(source=source(name="A", ident="a"), backend="rotector"))
b = queue.add(ScanJob(source=source(name="B", ident="b"), backend="rotector"))
c = queue.add(ScanJob(source=source(name="C", ident="c"), backend="rotector"))

assert [j.id for j in queue.runnable()] == [a.id, b.id], "two slots, two jobs"
assert queue.start(a.id) and queue.start(b.id)
assert queue.slots() == 0 and queue.runnable() == []
assert not queue.start(c.id), "a third must wait for a slot"
ok("concurrency is capped; the rest wait rather than piling on the budget")

assert queue.finish(a.id)
assert a.state is JobState.DONE and a.finished_at is not None
assert [j.id for j in queue.runnable()] == [c.id], "the slot goes to the next in line"
ok("finishing a job frees its slot for the next one")


# --- pausing keeps everything ---------------------------------------------
queue = JobQueue(max_concurrent=1)
job = queue.add(ScanJob(source=source(), backend="rotector"))
queue.start(job.id)
job.rows["1000"] = object()
time.sleep(0.05)

assert queue.pause(job.id)
banked = job.elapsed
assert job.state is JobState.PAUSED
assert banked > 0, "a paused job must remember the time it already spent"
assert job.rows, "a paused job keeps its results"
time.sleep(0.05)
assert job.elapsed == banked, "a paused clock does not keep running"
ok(f"pausing banks the clock ({banked:.3f}s) and keeps the rows")

assert queue.resume(job.id) and job.state is JobState.PENDING
assert queue.start(job.id) and job.state is JobState.RUNNING
time.sleep(0.05)
assert job.elapsed > banked, "resuming carries on from where it stopped"
ok("resuming continues the same clock rather than restarting it")


# --- prioritising ---------------------------------------------------------
queue = JobQueue(max_concurrent=1)
first = queue.add(ScanJob(source=source(name="A", ident="a"), backend="rotector"))
second = queue.add(ScanJob(source=source(name="B", ident="b"), backend="rotector"))
third = queue.add(ScanJob(source=source(name="C", ident="c"), backend="rotector"))
queue.start(first.id)

assert [j.id for j in queue.runnable()] == [], "no slots while one runs"
assert queue.prioritise(third.id)
waiting = [j for j in queue.order() if j.state is JobState.PENDING]
assert waiting[0].id == third.id, [j.id for j in waiting]
assert first.state is JobState.RUNNING, (
    "promoting a queued job must not stop one that is already running"
)
ok("prioritising puts a job next in line without interrupting what is running")

# and a promotion survives more jobs arriving afterwards
fourth = queue.add(ScanJob(source=source(name="D", ident="d"), backend="rotector"))
waiting = [j for j in queue.order() if j.state is JobState.PENDING]
assert waiting[0].id == third.id, "a later arrival jumped a promoted job"
ok("the promotion holds when new jobs are queued behind it")

# giving one job the whole budget pauses the others, reversibly
queue = JobQueue(max_concurrent=3)
x = queue.add(ScanJob(source=source(name="X", ident="x"), backend="rotector"))
y = queue.add(ScanJob(source=source(name="Y", ident="y"), backend="rotector"))
z = queue.add(ScanJob(source=source(name="Z", ident="z"), backend="rotector"))
for job in (x, y, z):
    queue.start(job.id)
paused = queue.pause_others(y.id)
assert {j.id for j in paused} == {x.id, z.id}
assert y.state is JobState.RUNNING
assert all(j.state is JobState.PAUSED for j in (x, z))
assert all(queue.resume(j.id) for j in (x, z))
ok("one job can take the whole budget, and the others come back")


# --- cancelling keeps the partial answer ----------------------------------
queue = JobQueue(max_concurrent=1)
job = queue.add(ScanJob(source=source(), backend="okappiki"))
queue.start(job.id)
job.rows.update({"1": object(), "2": object()})
assert queue.cancel(job.id)
assert job.state is JobState.CANCELLED and job.state.finished
assert len(job.rows) == 2, (
    "a stopped scan still answered about the members it reached; throwing that "
    "away would contradict the coverage the app reports everywhere else"
)
assert not queue.cancel(job.id), "cancelling twice is not a thing"
ok("cancelling keeps the partial results, which are still an answer")


# --- tidying up -----------------------------------------------------------
queue = JobQueue(max_concurrent=2)
done = queue.add(ScanJob(source=source(name="A", ident="a"), backend="rotector"))
live = queue.add(ScanJob(source=source(name="B", ident="b"), backend="rotector"))
queue.start(live.id)
queue.finish(done.id)

assert queue.remove(live.id) is None, "a running job cannot be removed from under itself"
assert queue.remove(done.id) is done
assert queue.active_id == live.id, "removing the shown job moves the view somewhere real"
ok("only finished jobs can be removed, and the view follows")

queue.finish(live.id)
assert len(queue.clear_finished()) == 1 and not queue.jobs
assert queue.active_id is None
ok("clearing finished jobs empties the queue and the view together")


# --- what the job list says -----------------------------------------------
queue = JobQueue(max_concurrent=2)
assert "No scans" in queue.summary()
running = queue.add(ScanJob(source=source(), backend="rotector"))
queue.start(running.id)
queue.add(ScanJob(source=source(ident="2"), backend="okappiki"))
line = queue.summary()
assert "1 running" in line and "1 queued" in line, line
running.note = "Checking members - 4,200 of 10,833"
assert "RUNNING" in running.describe() and "4,200" in running.describe()
queue.finish(running.id, error="Rotector API: rate limited")
assert running.state is JobState.FAILED
assert "FAILED" in running.describe() and "rate limited" in running.describe()
ok(f"the queue and each job describe themselves: {line!r}")


# --- the queue popup ------------------------------------------------------
# A popup rather than a pane, in the same shape as settings and export: it
# opens over the results, does one thing and gets out of the way.

import asyncio  # noqa: E402

from rsb.config import Config  # noqa: E402
from rsb.tui.dialogs import JobsDialog  # noqa: E402
from rsb.tui.proxies import ProxyTesterApp  # noqa: E402


async def _popup():
    from textual.widgets import DataTable

    queue = JobQueue(max_concurrent=2)
    first = queue.add(ScanJob(source=source(name="Hub", ident="1"),
                              backend="rotector"))
    twin = queue.add(ScanJob(source=source(name="Hub", ident="1"),
                             backend="okappiki"))
    other = queue.add(ScanJob(source=source(name="Bloxburg", ident="2"),
                              backend="rotector"))
    queue.start(first.id)
    queue.start(twin.id)
    first.rows.update({str(i): object() for i in range(120)})
    first.expected = 10833

    pool = BudgetPool()
    pool.register("rotector", RateLimiter())
    pool.register("okappiki", RateLimiter(limit=5, window=1.0, reserve=0))

    config = Config()
    config.token = "x"
    config.proxy.file = "/nonexistent"
    app = ProxyTesterApp(config, persist_theme=False)

    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause(0.3)
        app.push_screen(JobsDialog(queue, pool))
        await pilot.pause(0.9)
        table = app.screen.query_one("#jobs", DataTable)
        assert table.row_count == 3, table.row_count
        ok("the popup lists every job, including one source on two backends")

        # pause and resume have to act on the same job twice running. Order
        # changes with state, so holding a row index would move the cursor onto
        # whatever slid into that position -- which is exactly what it did.
        table.move_cursor(row=0)
        await pilot.press("p")
        await pilot.pause(0.3)
        assert first.state is JobState.PAUSED, first.state
        await pilot.press("p")
        await pilot.pause(0.3)
        assert first.state is JobState.PENDING, (
            "the cursor did not stay on the job it just paused"
        )
        assert first.rows, "pausing threw the results away"
        ok("pause and resume act on the same job, and it keeps its rows")

        table.move_cursor(row=2)
        await pilot.press("t")
        await pilot.pause(0.3)
        waiting = [j for j in queue.order() if j.state is JobState.PENDING]
        assert waiting[0].id == other.id, [j.source_name for j in waiting]
        assert twin.state is JobState.RUNNING, "promoting stopped a running scan"
        ok("prioritising moves a job to the front without stopping anything")

        await pilot.press("x")
        await pilot.pause(0.3)
        assert other.state is JobState.CANCELLED
        await pilot.press("d")
        await pilot.pause(0.3)
        assert queue.get(other.id) is None, "the cursor lost the job it stopped"
        assert len(queue.jobs) == 2
        ok("stopping then removing works on the job under the cursor")


asyncio.run(_popup())


# --- the results pane reads through to the active job ---------------------

async def _tabs():
    import rsb.tui.app as appmod
    from rsb.discord.gateway import GuildMember
    from rsb.discord.http import Channel, Guild
    from rsb.rotector import MemberReport
    from rsb.tui.app import Row, ScannerApp

    class _HTTP:
        def __init__(self, token, **kw): pass
        async def me(self): return {"username": "you", "global_name": "You", "id": "1"}
        async def guilds(self):
            return [Guild(id="1", name="Hub", owner=False, permissions=0,
                          member_count=10, presence_count=2)]
        async def relationships(self): return []
        async def private_channels(self): return []
        async def channels(self, gid):
            return [Channel(id="c", name="general", type=0, position=0,
                            everyone_can_view=True)]
        async def aclose(self): pass

    class _Gateway:
        def __init__(self, token, bot=False):
            self.user = None; self.on_reconnect = None; self.on_reconnected = None
        async def connect(self, timeout=45.0): return {}
        async def fetch_members(self, *a, **kw): return {}
        async def close(self): pass

    appmod.DiscordHTTP = _HTTP
    appmod.DiscordGateway = _Gateway

    config = Config()
    config.token = "fake.test.token"
    config.proxy.file = "/nonexistent"
    config.update.check_on_start = False
    app = ScannerApp(config, persist_theme=False)

    async with app.run_test(size=(140, 44)) as pilot:
        for _ in range(40):
            await pilot.pause(0.1)
            if app.rotector is not None:
                break

        strip = app.query_one("#job-tabs")
        assert "visible" not in strip.classes, "one tab is chrome saying nothing"

        hub = source(name="Hub", ident="1")
        first = app.job_for(hub, "rotector")
        app.queue.active_id = first.id
        first.rows["100"] = Row(member=GuildMember(id="100", username="alice"),
                                report=MemberReport(discord_id="100"))
        second = app.job_for(hub, "okappiki")
        app.refresh_tabs()
        await pilot.pause(0.5)

        assert first.id != second.id, "one source on two backends is one job"
        assert "visible" in strip.classes, "the strip appears once there is a choice"
        ok("the same server on two backends gets two tabs")

        # the pane reads through, so switching swaps what every call site sees
        app.show_job(second.id)
        await pilot.pause(0.4)
        assert len(app.rows) == 0, len(app.rows)
        app.show_job(first.id)
        await pilot.pause(0.4)
        assert len(app.rows) == 1, len(app.rows)
        ok("switching tabs swaps the results the whole app reads")

        # selection and view state belong to the tab that made them
        app.selected.add("100")
        app.search_term = "alice"
        app.show_job(second.id)
        await pilot.pause(0.3)
        assert not app.selected and app.search_term == "", (
            "a selection followed the tab switch; a bulk action would have "
            "offered members the visible scan never looked at"
        )
        app.show_job(first.id)
        await pilot.pause(0.3)
        assert app.selected == {"100"} and app.search_term == "alice"
        ok("selection, filter and search stay with the tab that made them")

        # re-scanning a finished source lands back in its own tab
        app.queue.finish(first.id)
        again = app.job_for(hub, "rotector")
        assert again is first, (
            "a completed source got a second tab; a merge would have found "
            "nothing to merge with"
        )
        ok("re-scanning a finished source reuses its tab, so a merge still merges")


asyncio.run(_tabs())

print("\nall scan queue checks passed.")
