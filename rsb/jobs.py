"""Scan jobs: a queue, a shared budget, and the rules for running several.

One scan at a time was the whole design until now -- ``scan_source`` was an
exclusive worker and the results pane was bound to a single set of rows. Running
several means each scan owning its own everything, and a queue deciding which
of them is allowed to be running.

Three rules shape this, and the first is not a preference:

* **The budget is shared, per backend.** Rotector's Terms of Use forbid
  circumventing the rate limit, and a limiter per job would multiply the
  request rate by however many scans somebody queued. :class:`BudgetPool` hands
  every job on a backend the *same* limiter, so ten concurrent scans are
  exactly as polite as one and simply finish slower. That is also what "share
  budget across scans" has to mean if the tool is to stay inside the window it
  documents.
* **Concurrency is small and deliberate.** Two jobs against one budget already
  halve each other; twenty just add scheduling noise and make every one of them
  look stalled. The cap is configurable and low.
* **Nothing is silently dropped.** A paused job keeps its results and its
  place. A cancelled one keeps what it had collected, because a partial scan is
  still an answer about the members it did reach -- which is the same reason
  the app reports coverage as a fraction rather than a checkmark.

This module holds no Textual and no I/O: it decides *what* should run, and the
app does the running. That is what makes it testable.
"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field
from enum import Enum

from .config import BACKENDS
from .ratelimit import RateLimiter


class JobState(str, Enum):
    """Where a job is. ``str`` so it renders and compares without ceremony."""

    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def finished(self) -> bool:
        return self in (JobState.DONE, JobState.FAILED, JobState.CANCELLED)

    @property
    def active(self) -> bool:
        """Holding a concurrency slot."""
        return self is JobState.RUNNING


#: what the results table shows for each state, worst-news-first when sorting
STATE_LABEL = {
    JobState.RUNNING: "RUNNING",
    JobState.PAUSED: "PAUSED",
    JobState.PENDING: "QUEUED",
    JobState.DONE: "DONE",
    JobState.FAILED: "FAILED",
    JobState.CANCELLED: "STOPPED",
}

_ids = itertools.count(1)


@dataclass
class ScanJob:
    """One scan of one source against one backend.

    A job owns its results rather than writing into a shared table, which is
    what lets the same server be scanned twice at once -- once per backend --
    without the two answers landing on top of each other.
    """

    source: object
    backend: str
    #: False lists members without looking any of them up
    lookup: bool = True
    #: what a re-scan does with rows this job already holds
    mode: str = "replace"

    id: int = field(default_factory=lambda: next(_ids))
    state: JobState = JobState.PENDING
    #: higher runs sooner; equal priority falls back to the order added
    priority: int = 0

    #: discord id -> Row, filled by the app as the scan publishes
    rows: dict = field(default_factory=dict)
    #: members seen so far and the total the source claims, for progress
    found: int = 0
    expected: int | None = None
    #: the last thing this job was doing, for the job list
    note: str = ""
    error: str | None = None

    queued_at: float = field(default_factory=time.monotonic)
    started_at: float | None = None
    finished_at: float | None = None
    #: seconds already spent running, so a pause does not reset the clock
    elapsed_before_pause: float = 0.0

    def __post_init__(self) -> None:
        if self.backend not in BACKENDS:
            raise ValueError(
                f"unknown backend {self.backend!r}; expected one of "
                f"{', '.join(BACKENDS)}"
            )

    # -- naming ------------------------------------------------------------

    @property
    def source_name(self) -> str:
        return getattr(self.source, "name", "?")

    @property
    def label(self) -> str:
        """What the tab says. The backend is in it because the whole point is
        that the same source can be here twice."""
        return f"{self.source_name} · {self.backend}"

    @property
    def title(self) -> str:
        kind = getattr(self.source, "label", "")
        return f"{self.source_name}{f' ({kind})' if kind else ''} · {self.backend}"

    # -- timing ------------------------------------------------------------

    @property
    def elapsed(self) -> float:
        """Seconds actually spent scanning, pauses excluded."""
        if self.started_at is None:
            return self.elapsed_before_pause
        if self.state is JobState.RUNNING:
            return self.elapsed_before_pause + (time.monotonic() - self.started_at)
        return self.elapsed_before_pause

    @property
    def progress(self) -> float | None:
        """0..1, or None when the source never said how many members it has."""
        if not self.expected:
            return None
        return min(1.0, self.found / self.expected)

    def describe(self) -> str:
        state = STATE_LABEL[self.state]
        if self.error:
            return f"{state} - {self.error}"
        if self.note:
            return f"{state} - {self.note}"
        if self.state is JobState.DONE:
            return f"{state} - {len(self.rows):,} member(s)"
        return state


class BudgetPool:
    """One rate limiter per backend, handed to every job on that backend.

    The alternative -- a limiter each -- would let three queued scans make
    three times the documented request rate, which is precisely the thing
    Rotector's terms prohibit and the proxy tester exists to make visible.
    Sharing means more scans finish slower rather than more scans getting
    the account blocked.
    """

    def __init__(self, limiters: dict[str, RateLimiter] | None = None) -> None:
        self._limiters: dict[str, RateLimiter] = dict(limiters or {})

    def register(self, backend: str, limiter: RateLimiter) -> None:
        self._limiters[backend] = limiter

    def limiter(self, backend: str) -> RateLimiter | None:
        return self._limiters.get(backend)

    def snapshot(self, backend: str):
        limiter = self._limiters.get(backend)
        return limiter.snapshot() if limiter is not None else None

    def backends(self) -> list[str]:
        return sorted(self._limiters)

    def sharing(self, jobs) -> dict[str, int]:
        """How many of ``jobs`` are drawing on each backend's budget.

        The job list shows this: two scans against one backend is not a
        problem, but it is the reason both look half as fast, and saying so
        beats leaving somebody to wonder.
        """
        counts: dict[str, int] = {}
        for job in jobs:
            if job.state is JobState.RUNNING:
                counts[job.backend] = counts.get(job.backend, 0) + 1
        return counts


class JobQueue:
    """The jobs, in order, and the rules about which may run.

    Deliberately passive: it never starts anything. The app asks
    :meth:`runnable` what it is allowed to begin and reports back through
    :meth:`start`, :meth:`finish` and friends. That keeps every ordering rule
    here, where it can be tested without a terminal.
    """

    def __init__(self, max_concurrent: int = 2) -> None:
        self.max_concurrent = max(1, max_concurrent)
        self.jobs: list[ScanJob] = []
        #: which job the results pane is showing
        self.active_id: int | None = None

    # -- membership --------------------------------------------------------

    def add(self, job: ScanJob) -> ScanJob:
        self.jobs.append(job)
        if self.active_id is None:
            self.active_id = job.id
        return job

    def get(self, job_id: int) -> ScanJob | None:
        for job in self.jobs:
            if job.id == job_id:
                return job
        return None

    def remove(self, job_id: int) -> ScanJob | None:
        """Drop a job entirely. Only ever a finished one -- a running job is
        cancelled first, so its worker is never left writing into a job the
        queue has forgotten."""
        job = self.get(job_id)
        if job is None or not job.state.finished:
            return None
        self.jobs.remove(job)
        if self.active_id == job_id:
            self.active_id = self.jobs[0].id if self.jobs else None
        return job

    def clear_finished(self) -> list[ScanJob]:
        gone = [j for j in self.jobs if j.state.finished]
        for job in gone:
            self.remove(job.id)
        return gone

    # -- views -------------------------------------------------------------

    @property
    def active(self) -> ScanJob | None:
        return self.get(self.active_id) if self.active_id is not None else None

    def running(self) -> list[ScanJob]:
        return [j for j in self.jobs if j.state is JobState.RUNNING]

    def pending(self) -> list[ScanJob]:
        return [j for j in self.jobs if j.state is JobState.PENDING]

    def paused(self) -> list[ScanJob]:
        return [j for j in self.jobs if j.state is JobState.PAUSED]

    def unfinished(self) -> list[ScanJob]:
        return [j for j in self.jobs if not j.state.finished]

    def order(self) -> list[ScanJob]:
        """Queue order: running first, then by priority, then by age.

        Age last so equal priorities keep the order they were added in --
        a queue that reshuffles itself is one nobody can plan against.
        """
        rank = {
            JobState.RUNNING: 0, JobState.PAUSED: 1, JobState.PENDING: 2,
            JobState.DONE: 3, JobState.FAILED: 3, JobState.CANCELLED: 3,
        }
        return sorted(
            self.jobs,
            key=lambda j: (rank[j.state], -j.priority, j.queued_at, j.id),
        )

    # -- scheduling --------------------------------------------------------

    def slots(self) -> int:
        return max(0, self.max_concurrent - len(self.running()))

    def runnable(self) -> list[ScanJob]:
        """Pending jobs the queue is willing to start, best first."""
        free = self.slots()
        if free <= 0:
            return []
        waiting = [j for j in self.order() if j.state is JobState.PENDING]
        return waiting[:free]

    def start(self, job_id: int) -> bool:
        job = self.get(job_id)
        if job is None or job.state not in (JobState.PENDING, JobState.PAUSED):
            return False
        if not self.running() or len(self.running()) < self.max_concurrent:
            job.state = JobState.RUNNING
            job.started_at = time.monotonic()
            job.error = None
            return True
        return False

    def pause(self, job_id: int) -> bool:
        """Stop a running job without losing what it has.

        The elapsed clock is banked rather than reset, so a job paused twice
        still reports the time it actually spent scanning.
        """
        job = self.get(job_id)
        if job is None or job.state is not JobState.RUNNING:
            return False
        job.elapsed_before_pause = job.elapsed
        job.started_at = None
        job.state = JobState.PAUSED
        return True

    def resume(self, job_id: int) -> bool:
        job = self.get(job_id)
        if job is None or job.state is not JobState.PAUSED:
            return False
        job.state = JobState.PENDING
        return True

    def cancel(self, job_id: int) -> bool:
        job = self.get(job_id)
        if job is None or job.state.finished:
            return False
        job.elapsed_before_pause = job.elapsed
        job.started_at = None
        job.state = JobState.CANCELLED
        job.finished_at = time.monotonic()
        return True

    def finish(self, job_id: int, error: str | None = None) -> bool:
        job = self.get(job_id)
        if job is None or job.state.finished:
            return False
        job.elapsed_before_pause = job.elapsed
        job.started_at = None
        job.state = JobState.FAILED if error else JobState.DONE
        job.error = error
        job.finished_at = time.monotonic()
        return True

    # -- ordering ----------------------------------------------------------

    def prioritise(self, job_id: int) -> bool:
        """Put a job at the head of the queue.

        Priority rather than list position, so the ordering survives anything
        else being added afterwards. Nothing running is stopped -- promoting a
        job says "next", and taking a slot from a scan mid-flight would throw
        away work to answer a question about order.
        """
        job = self.get(job_id)
        if job is None or job.state.finished:
            return False
        highest = max((j.priority for j in self.jobs if j.id != job_id), default=0)
        job.priority = highest + 1
        return True

    def deprioritise(self, job_id: int) -> bool:
        job = self.get(job_id)
        if job is None or job.state.finished:
            return False
        lowest = min((j.priority for j in self.jobs if j.id != job_id), default=0)
        job.priority = lowest - 1
        return True

    def pause_others(self, job_id: int) -> list[ScanJob]:
        """Give one job the whole budget: pause every other running scan.

        The obvious meaning of "prioritise this one" when something is already
        running, and reversible -- the others keep their rows and their place.
        """
        paused = []
        for job in self.running():
            if job.id != job_id and self.pause(job.id):
                paused.append(job)
        return paused

    # -- reporting ---------------------------------------------------------

    def summary(self) -> str:
        counts: dict[JobState, int] = {}
        for job in self.jobs:
            counts[job.state] = counts.get(job.state, 0) + 1
        if not counts:
            return "No scans queued."
        parts = [
            f"{counts[state]} {STATE_LABEL[state].lower()}"
            for state in (JobState.RUNNING, JobState.PAUSED, JobState.PENDING,
                          JobState.DONE, JobState.FAILED, JobState.CANCELLED)
            if counts.get(state)
        ]
        return ", ".join(parts)

    def duplicate_of(self, source_id: str, backend: str) -> ScanJob | None:
        """An unfinished job already covering this source and backend.

        Queueing the same pair twice is almost always a mis-click; the same
        source on a *different* backend is the feature, so only the pair
        counts as a duplicate.
        """
        for job in self.unfinished():
            if getattr(job.source, "id", None) == source_id and job.backend == backend:
                return job
        return None
