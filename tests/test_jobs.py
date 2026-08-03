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

print("\nall scan queue checks passed.")
