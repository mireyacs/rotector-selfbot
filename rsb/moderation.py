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
from .triage import DEFAULT_POLICY, Recommendation, TriagePolicy, triage
from .verdict import (
    Verdict,
    category_name,
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
    appeal=None,
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

    own = ""
    if appeal is not None and getattr(appeal, "include_in_reason", False):
        own = (appeal.invite or appeal.contact or "").strip()
        if own:
            candidate = " ".join(f"{text} | Also: {own}".split())
            # same rule as the notice: an optional route never displaces the
            # required one, and never costs the finding its detail
            if len(candidate) <= MAX_REASON:
                text = candidate

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
    #: CAUTION means Rotector found *something* but not enough to conclude.
    #: It is the one tier where acting is defensible and still might be wrong,
    #: so it is permitted -- behind a second, separate confirmation that
    #: cannot be satisfied by the same click as the first.
    needs_double_confirm: bool = False


def check_eligibility(
    report: MemberReport,
    require_threat: bool = True,
    allow_caution: bool = True,
    policy: TriagePolicy | None = None,
) -> Eligibility:
    """Whether this member may be actioned, and how strongly to warn.

    The decision is :mod:`rsb.triage`'s, not this function's. That matters most
    under the Okappiki backend, where the old rule -- *anything an unverified
    database flagged is CAUTION* -- put nearly every finding in one undivided
    pile. The rule table separates the piles and names the rule it used, so an
    explanation here is a citation rather than a restatement.
    """
    result = triage(report, policy or DEFAULT_POLICY)
    cite = f"[{result.rule}] "

    if result.recommendation is Recommendation.BAN:
        return Eligibility(True, False, cite + result.headline)

    if result.recommendation is Recommendation.CAUTION and allow_caution:
        # Real evidence that no database will conclude from. Acting is a
        # judgement call, so it is allowed -- but only through a second
        # confirmation, because the honest description of it is "might be wrong".
        return Eligibility(
            True,
            True,
            cite
            + result.headline
            + " Acting on it is your judgement, not the database's.",
            needs_double_confirm=True,
        )

    return Eligibility(not require_threat, True, cite + result.headline)


#: default text sent to someone before they are actioned. Deliberately plain:
#: it states what is happening, why, and how to contest it, and claims nothing
#: Rotector does not.
DEFAULT_NOTICE = (
    "You are being {action} from {place}.\n\n"
    "A Roblox account linked to your Discord account has been flagged in the "
    "Rotector database: {reason}\n\n"
    "If you believe this is a mistake, you can appeal at "
    "https://rotector.com - the appeal goes to Rotector, not to this server."
)

#: how long the notice may be (Discord's message limit)
MAX_NOTICE = 2000


def appeal_lines(appeal) -> list[str]:
    """Your own appeal route, rendered for a message.

    Always *additional*. Rotector's terms require that anyone actioned on their
    data can appeal to Rotector, so a second route is offered alongside rather
    than instead -- somebody told only to contact you would have lost the
    ability to correct the database that flagged them.
    """
    if appeal is None or not getattr(appeal, "configured", False):
        return []
    lines = ["", "", "You can also appeal directly to us:"]
    if appeal.invite.strip():
        lines.append(f"  {appeal.invite.strip()}")
    if appeal.contact.strip():
        lines.append(f"  {appeal.contact.strip()}")
    if appeal.note.strip():
        lines.append(appeal.note.strip())
    return lines


def build_notice(
    report: MemberReport,
    action: str,
    place: str,
    template: str = DEFAULT_NOTICE,
    appeal=None,
) -> str:
    """Compose the message sent to someone *before* they are actioned.

    Sending it first is the whole point: once somebody is banned there is no
    shared server left, and a DM can no longer reach them. A notice that
    arrives after the door is shut is not a notice.
    """
    finding = summarise_finding(report)
    fields = {"action": action, "place": place, "reason": finding}
    try:
        text = template.format(**fields)
    except (KeyError, IndexError):
        # a template with stray braces should not cost somebody their notice
        text = f"{template}\n\n{action} - {place} - {finding}"

    if "rotector.com" not in text.lower():
        text = f"{text}\n\nYou can appeal at https://rotector.com"

    extra = appeal_lines(appeal)
    if extra:
        candidate = text + "\n".join(extra)
        # only if it fits: the Rotector route is required and must never be
        # the thing that gets trimmed to make room for the optional one
        if len(candidate) <= MAX_NOTICE:
            text = candidate
    return text[:MAX_NOTICE]


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
    allow_caution: bool = True,
    gated: bool = True,
    template: str = "Flagged by Rotector: {reason}",
    custom: str | None = None,
    past: str = "",
    appeal=None,
    policy: TriagePolicy | None = None,
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
            eligibility = check_eligibility(
                report,
                require_threat=require_threat,
                allow_caution=allow_caution,
                policy=policy,
            )
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
            reason=build_reason(report, template, custom, appeal=appeal),
        )
        (plan.allowed if eligibility.allowed else plan.blocked).append(target)
    return plan


#: filters a bulk action can be aimed at, worst first
#:
#: ``ban`` and ``threat`` are not the same set and the difference is the point.
#: ``threat`` is one database's verdict on one account; ``ban`` is what the rule
#: table concluded from all of them, so it also picks up the members Rotector
#: flagged inconclusively and another database independently confirmed.
BULK_SCOPES: list[tuple[str, str]] = [
    ("selected", "Only the members I selected"),
    ("ban", "Everyone the rule table says a ban is supported for"),
    ("threat", "Everyone with a THREAT verdict"),
    ("caution", "Everyone at CAUTION or worse"),
    ("filtered", "Everyone the current filter shows"),
]


def rows_for_scope(scope: str, selected_rows, filtered_rows, policy=None):
    """Resolve a bulk scope to the rows it covers."""
    if scope == "selected":
        return list(selected_rows)
    if scope == "ban":
        return [
            r
            for r in filtered_rows
            if triage(r.report, policy or DEFAULT_POLICY).recommendation
            is Recommendation.BAN
        ]
    if scope == "threat":
        return [r for r in filtered_rows if r.report.verdict is Verdict.THREAT]
    if scope == "caution":
        return [r for r in filtered_rows if r.report.verdict >= Verdict.CAUTION]
    return list(filtered_rows)
