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

from dataclasses import dataclass

from .discord.http import MAX_REASON
from .rotector import MemberReport
from .verdict import Verdict, category_name, flag_is_actionable, flag_name

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
