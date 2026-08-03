"""Flag-type semantics from the Rotector API, mapped onto UI verdicts.

Reference: https://roscoe.rotector.com/docs

Two rules from Rotector's Terms of Use shape this module and must not be
relaxed:

* Rule 3 -- a user whose flag type is ``Unflagged`` must **not** be presented
  as "Safe".  It only means nothing has been detected *yet*.  Hence the
  ``NO_DETECTIONS`` verdict name.
* The docs' own warning -- do not treat every non-zero flag type as
  inappropriate.  Only types 1 (Flagged) and 2 (Confirmed) are safe to action
  automatically; 0, 3, 4, 6 and 8 are explicitly not actionable.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

ATTRIBUTION = "Data: Rotector (https://rotector.com) - appeals at rotector.com"


class FlagType(IntEnum):
    UNFLAGGED = 0
    FLAGGED = 1
    CONFIRMED = 2
    QUEUED = 3
    PROVISIONAL = 4
    MIXED = 5
    PAST_OFFENDER = 6
    REDACTED = 8


#: flag type -> (display name, actionable, description)
FLAG_META: dict[int, tuple[str, bool, str]] = {
    0: ("Unflagged", False, "No violations detected yet"),
    1: ("Flagged", True, "System-detected violations with high accuracy"),
    2: ("Confirmed", True, "Violations manually verified by a moderator"),
    3: ("Queued", False, "Submitted for review, not an inappropriate user"),
    4: ("Provisional Flag", False, "Flagged but reasons hidden pending human review"),
    5: ("Mixed", False, "Some inappropriate activity but insufficient evidence"),
    6: ("Past Offender", False, "Previously flagged but has since cleared violations"),
    8: ("Redacted", False, "Reasons and evidence withheld under a privacy restriction"),
}

CATEGORY_NAMES: dict[int, str] = {
    1: "CSAM",
    2: "Sexual",
    3: "Kink",
    4: "Raceplay",
    5: "Condo",
    6: "Other",
}

#: verification source for a Discord <-> Roblox connection
SOURCE_NAMES: dict[int, str] = {
    0: "Deprecated",
    1: "Unknown",
    2: "Profile",
}


class Verdict(IntEnum):
    """Ordered severity, so ``max()`` picks the worst of several accounts."""

    UNKNOWN = 0
    NO_DETECTIONS = 1
    INFO = 2
    CAUTION = 3
    THREAT = 4


#: verdict -> (short label, rich style, one-line meaning)
VERDICT_META: dict[Verdict, tuple[str, str, str]] = {
    Verdict.UNKNOWN: (
        "UNKNOWN",
        "dim white",
        "No linked Roblox account is known to Rotector - nothing can be said.",
    ),
    Verdict.NO_DETECTIONS: (
        "NO DETECTIONS",
        "green",
        "Linked account(s) known but unflagged. Not a clean bill of health.",
    ),
    Verdict.INFO: (
        "INFO",
        "cyan",
        "Queued / past offender / redacted. Informational only, not actionable.",
    ),
    Verdict.CAUTION: (
        "CAUTION",
        "yellow",
        "Mixed or provisional signals. Review manually before acting.",
    ),
    Verdict.THREAT: (
        "THREAT",
        "bold red",
        "Flagged or Confirmed violations. Safe to action per Rotector.",
    ),
}

#: flag type -> verdict
_FLAG_TO_VERDICT: dict[int, Verdict] = {
    0: Verdict.NO_DETECTIONS,
    1: Verdict.THREAT,
    2: Verdict.THREAT,
    3: Verdict.INFO,
    4: Verdict.CAUTION,
    5: Verdict.CAUTION,
    6: Verdict.INFO,
    8: Verdict.INFO,
}


def flag_name(flag_type: int | None) -> str:
    if flag_type is None:
        return "Untracked"
    return FLAG_META.get(flag_type, (f"Unknown({flag_type})", False, ""))[0]


def flag_is_actionable(flag_type: int | None) -> bool:
    if flag_type is None:
        return False
    return FLAG_META.get(flag_type, ("", False, ""))[1]


def verdict_for_flag(flag_type: int | None) -> Verdict:
    if flag_type is None:
        return Verdict.UNKNOWN
    return _FLAG_TO_VERDICT.get(flag_type, Verdict.INFO)


def verdict_label(verdict: Verdict) -> str:
    return VERDICT_META[verdict][0]


def verdict_style(verdict: Verdict) -> str:
    return VERDICT_META[verdict][1]


def verdict_meaning(verdict: Verdict) -> str:
    return VERDICT_META[verdict][2]


def category_name(category: int | None) -> str | None:
    if category is None:
        return None
    return CATEGORY_NAMES.get(category, f"Category {category}")


def source_names(sources: list[int] | None) -> str:
    if not sources:
        return "unknown"
    return ", ".join(SOURCE_NAMES.get(s, str(s)) for s in sources)


# --------------------------------------------------------------------------
# multi-source findings
# --------------------------------------------------------------------------


@dataclass
class Signal:
    """One named source's answer about one Discord account.

    Rotector answers per *Roblox account*, which is why a report normally
    carries :class:`~rsb.rotector.RobloxAccount` objects. A backend that
    aggregates several services answers per *source* instead, and some of those
    sources -- Okappiki's own list, mococo -- flag a Discord id with no Roblox
    link attached at all. Those findings have nowhere to live on an account, so
    they live here, and ``MemberReport.verdict`` takes the worst of both.

    ``verdict`` is set by whoever builds the signal rather than derived, because
    what a flag *means* differs per source: only Rotector publishes flag types
    with documented actionability, so only Rotector's can reach THREAT.
    """

    #: the service that said it: "rotector", "okappiki", "mococo"
    source: str
    flagged: bool
    verdict: Verdict
    #: the source's own words, unedited. Undocumented services phrase these
    #: however they like, so it is shown rather than parsed.
    reason: str = ""
    #: mococo publishes a numeric score; the others do not
    score: int | None = None
    roblox_id: int | None = None
    roblox_username: str | None = None
    flag_type: int | None = None

    @property
    def label(self) -> str:
        return self.source.title()
