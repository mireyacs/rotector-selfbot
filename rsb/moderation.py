"""Kick/ban support: reason construction and eligibility.

Two rules are enforced here rather than left to the operator.

* **Attribution.** Rotector's Terms of Use require that any action taken on
  their data attributes Rotector, or links rotector.com so the affected person
  can appeal. Every reason therefore carries the appeal link, including reasons
  the user typed themselves.
* **Actionability.** Rotector documents only flag types Flagged and Confirmed
  as safe to action automatically; 0, 3, 4, 6 and 8 are explicitly not. Acting
  on anything else is possible but is reported as needing a deliberate override,
  never as routine.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .discord.http import MAX_REASON
from .rotector import MemberReport
from .verdict import (
    Verdict,
    category_name,
    flag_is_actionable,
    flag_name,
    verdict_label,
)

APPEAL_NOTE = "Appeal at rotector.com"


def summarise_finding(report: MemberReport) -> str:
    """A short, factual description of why this member was flagged."""
    account = report.worst_account
    if account is None:
        return "no Rotector finding"

    parts = [flag_name(account.flag_type)]
    if (category := category_name(account.category)) is not None:
        parts.append(category)
    if account.reasons:
        parts.append(", ".join(list(account.reasons)[:2]))
    parts.append(f"Roblox {account.username} ({account.user_id})")
    return " - ".join(parts)


def build_reason(
    report: MemberReport,
    template: str = "Flagged by Rotector: {reason}",
    custom: str | None = None,
) -> str:
    """Compose an audit-log reason, always carrying the appeal link."""
    if custom and custom.strip():
        text = custom.strip()
    else:
        finding = summarise_finding(report)
        try:
            text = template.format(reason=finding)
        except (KeyError, IndexError):
            # a template with stray braces should not break the action
            text = f"{template} {finding}".strip()

    if "rotector.com" not in text.lower():
        text = f"{text} | {APPEAL_NOTE}"

    text = " ".join(text.split())
    if len(text) > MAX_REASON:
        # Trim the body, never the appeal link. The suffix length is measured
        # rather than guessed -- Discord rejects reasons past MAX_REASON, and
        # being one character over is just as fatal as being a hundred.
        suffix = f"... | {APPEAL_NOTE}"
        text = text[: MAX_REASON - len(suffix)].rstrip() + suffix
    return text[:MAX_REASON]


@dataclass
class Eligibility:
    allowed: bool
    needs_override: bool
    explanation: str


def check_eligibility(report: MemberReport, require_threat: bool = True) -> Eligibility:
    """Whether this member may be actioned, and how strongly to warn."""
    account = report.worst_account
    flag = account.flag_type if account else None
    verdict = report.verdict

    if flag is not None and flag_is_actionable(flag):
        return Eligibility(
            True,
            False,
            f"{flag_name(flag)} - documented by Rotector as safe to action.",
        )

    if verdict is Verdict.UNKNOWN:
        detail = (
            "Rotector knows of no Roblox account for this user. There is no "
            "finding here at all - nothing supports acting on them."
        )
    elif verdict is Verdict.NO_DETECTIONS:
        detail = (
            "Rotector has not flagged this user. 'No detections' is not "
            "evidence of anything; acting on it is acting on nothing."
        )
    else:
        detail = (
            f"{flag_name(flag)} is explicitly not actionable per Rotector's "
            f"documentation - the finding is informational, not a conclusion."
        )

    return Eligibility(not require_threat, True, detail)


# --------------------------------------------------------------------------
# bulk
# --------------------------------------------------------------------------


@dataclass
class BulkTarget:
    """One member in a bulk plan, and whether it may proceed."""

    member_id: str
    label: str
    verdict: Verdict
    eligibility: Eligibility
    reason: str


@dataclass
class BulkPlan:
    """What a bulk action would do, split into what may and may not proceed."""

    action: str
    allowed: list[BulkTarget] = field(default_factory=list)
    blocked: list[BulkTarget] = field(default_factory=list)
    #: past tense of the action, e.g. "banned" -- supplied rather than derived
    #: because English does not let you get there from "ban" by adding "ed"
    past: str = ""

    @property
    def total(self) -> int:
        return len(self.allowed) + len(self.blocked)

    def describe(self) -> str:
        if not self.total:
            return "Nothing matches."
        text = (f"{len(self.allowed):,} of {self.total:,} may be "
                f"{self.past or 'actioned'}")
        if self.blocked:
            text += (
                f"; {len(self.blocked):,} blocked because the finding does not "
                f"support it"
            )
        return text


def plan_bulk(
    rows,
    action: str,
    *,
    require_threat: bool = True,
    gated: bool = True,
    template: str = "Flagged by Rotector: {reason}",
    custom: str | None = None,
    past: str = "",
) -> BulkPlan:
    """Work out who a bulk action may touch, before anything happens.

    Splitting the set up front is the point: acting on fifty people at once is
    exactly where a mistake is least recoverable, so the ineligible ones are
    identified and set aside rather than discovered halfway through.
    """
    plan = BulkPlan(action=action, past=past)
    for row in rows:
        report = row.report
        if gated:
            eligibility = check_eligibility(report, require_threat=require_threat)
        else:
            eligibility = Eligibility(
                True,
                False,
                f"{verdict_label(report.verdict)}. This one is your call.",
            )
        target = BulkTarget(
            member_id=row.member.id,
            label=row.member.display_name,
            verdict=report.verdict,
            eligibility=eligibility,
            reason=build_reason(report, template, custom),
        )
        (plan.allowed if eligibility.allowed else plan.blocked).append(target)
    return plan


#: verdict filters a bulk action can be aimed at, worst first
BULK_SCOPES: list[tuple[str, str]] = [
    ("selected", "Only the members I selected"),
    ("threat", "Everyone with a THREAT verdict"),
    ("caution", "Everyone at CAUTION or worse"),
    ("filtered", "Everyone the current filter shows"),
]


def rows_for_scope(scope: str, selected_rows, filtered_rows):
    """Resolve a bulk scope to the rows it covers."""
    if scope == "selected":
        return list(selected_rows)
    if scope == "threat":
        return [r for r in filtered_rows if r.report.verdict is Verdict.THREAT]
    if scope == "caution":
        return [r for r in filtered_rows if r.report.verdict >= Verdict.CAUTION]
    return list(filtered_rows)
