"""Deleting your own messages from a conversation with a flagged account.

Scope, stated plainly because it is easy to assume otherwise: this deletes
**only your own messages**. Discord gives no way to remove someone else's
messages from a DM, so their side of the conversation stays exactly where it
is. What this achieves is removing *your* contributions -- which is what you
can actually control.

Two phases, always in this order:

* :func:`plan_purge` reads the history and reports what *would* go. Nothing is
  deleted. This is what the UI shows before asking for confirmation, because
  message deletion cannot be undone.
* :func:`execute_purge` deletes the planned messages, paced so Discord's
  per-channel delete limit is not tripped, and stoppable part-way through.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

#: messages per history page
PAGE_SIZE = 100
#: pause between history pages, to stay clear of the read limit
PAGE_DELAY = 0.35
#: pause between deletions; Discord's per-channel delete bucket is strict
DEFAULT_DELETE_DELAY = 1.0

ProgressFn = Callable[[str, int, int], None]

KIND_DM = "dm"
KIND_GROUP = "group"


@dataclass
class PurgeTarget:
    channel_id: str
    label: str
    kind: str = KIND_DM

    @property
    def is_group(self) -> bool:
        return self.kind == KIND_GROUP


@dataclass
class PlannedMessage:
    id: str
    timestamp: str
    preview: str

    @property
    def when(self) -> str:
        return self.timestamp[:19].replace("T", " ")


@dataclass
class PurgePlan:
    target: PurgeTarget
    messages: list[PlannedMessage] = field(default_factory=list)
    scanned: int = 0
    pages: int = 0
    reached_end: bool = True
    oldest: str | None = None
    newest: str | None = None

    @property
    def count(self) -> int:
        return len(self.messages)

    def describe(self) -> str:
        if not self.messages:
            return f"No messages of yours in {self.target.label}."
        span = ""
        if self.oldest and self.newest:
            span = f", {self.oldest[:10]} to {self.newest[:10]}"
        tail = "" if self.reached_end else " (history truncated by the limits set)"
        return (
            f"{self.count:,} of your messages in {self.target.label} "
            f"(out of {self.scanned:,} scanned{span}){tail}"
        )


@dataclass
class PurgeResult:
    deleted: int = 0
    failed: int = 0
    stopped: bool = False
    errors: list[str] = field(default_factory=list)

    def describe(self) -> str:
        bits = [f"{self.deleted:,} deleted"]
        if self.failed:
            bits.append(f"{self.failed:,} could not be deleted")
        if self.stopped:
            bits.append("stopped early")
        return ", ".join(bits)


def _preview(content: str, width: int = 60) -> str:
    text = " ".join((content or "").split())
    if not text:
        return "(no text - attachment or embed)"
    return text if len(text) <= width else text[: width - 1] + "…"


async def plan_purge(
    http,
    target: PurgeTarget,
    own_id: str,
    max_messages: int = 0,
    max_age_days: int = 0,
    on_progress: ProgressFn | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> PurgePlan:
    """Walk the history and collect your own messages. Deletes nothing.

    ``max_messages`` caps how many of *your* messages are collected;
    ``max_age_days`` stops the walk once history older than that is reached.
    Both default to unlimited.
    """
    plan = PurgePlan(target=target)
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=max_age_days)
        if max_age_days
        else None
    )
    before: str | None = None

    while True:
        if should_stop and should_stop():
            plan.reached_end = False
            break

        page = await http.channel_messages(
            target.channel_id, limit=PAGE_SIZE, before=before
        )
        if not page:
            break
        plan.pages += 1
        plan.scanned += len(page)

        hit_cutoff = False
        for message in page:
            timestamp = str(message.get("timestamp") or "")
            if cutoff and timestamp:
                try:
                    when = datetime.fromisoformat(timestamp)
                    if when < cutoff:
                        hit_cutoff = True
                        break
                except ValueError:
                    pass

            author = (message.get("author") or {}).get("id")
            if str(author) != str(own_id):
                continue
            plan.messages.append(
                PlannedMessage(
                    id=str(message.get("id")),
                    timestamp=timestamp,
                    preview=_preview(message.get("content") or ""),
                )
            )
            if max_messages and len(plan.messages) >= max_messages:
                plan.reached_end = False
                hit_cutoff = True
                break

        if on_progress:
            on_progress(
                f"Reading history of {target.label}", plan.count, plan.scanned
            )

        if hit_cutoff:
            break
        if len(page) < PAGE_SIZE:
            break
        before = str(page[-1].get("id"))
        await asyncio.sleep(PAGE_DELAY)

    if plan.messages:
        stamps = sorted(m.timestamp for m in plan.messages if m.timestamp)
        if stamps:
            plan.oldest, plan.newest = stamps[0], stamps[-1]
    return plan


async def execute_purge(
    http,
    plan: PurgePlan,
    delete_delay: float = DEFAULT_DELETE_DELAY,
    on_progress: ProgressFn | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> PurgeResult:
    """Delete the planned messages, oldest first, paced and interruptible."""
    result = PurgeResult()
    total = plan.count
    if not total:
        return result

    # Oldest first: if it is interrupted, what survives is the recent tail,
    # which is the part you are most likely to still want a record of.
    ordered = sorted(plan.messages, key=lambda m: m.timestamp)

    for index, message in enumerate(ordered, start=1):
        if should_stop and should_stop():
            result.stopped = True
            break
        try:
            await http.delete_message(plan.target.channel_id, message.id)
            result.deleted += 1
        except Exception as exc:  # noqa: BLE001 - one failure must not stop the rest
            result.failed += 1
            if len(result.errors) < 5:
                result.errors.append(f"{message.id}: {type(exc).__name__}")
        if on_progress:
            on_progress(
                f"Deleting your messages in {plan.target.label}", index, total
            )
        if index < total and delete_delay:
            await asyncio.sleep(delete_delay)

    return result
