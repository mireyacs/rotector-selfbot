"""Deterministic triage: cautious, or actually ban?

:mod:`rsb.verdict` grades *evidence* -- what the databases found and how
strongly. This module answers the separate question a moderator actually has in
front of them: **should staff be cautious with this person, or does the evidence
support banning them?**

Those are not the same question, and collapsing them is what makes a results
table useless. Under the Okappiki backend every unverified sighting lands on
``Verdict.CAUTION``, correctly -- neither Detective Okappiki nor mococo
publishes a claim that their sighting is safe to act on. But that means a person
seen once by one crawler and a person all three databases agree on both read
CAUTION, and a column where nearly every finding says the same word is a column
nobody reads.

Deterministic here means literally that: an ordered table of rules, evaluated
top to bottom, first match wins, and the result names the rule that fired so a
moderator can look it up and disagree with it. No scoring heuristics, no
reason-text keyword matching, no thresholds pulled from nowhere.

What each party actually publishes
----------------------------------

The three are not interchangeable, and treating them as three equal votes would
be the central mistake:

* **Rotector** publishes a ``flag_type`` with *documented actionability*. Its
  docs name types 1 and 2 as safe to action and warn against treating every
  non-zero type as inappropriate -- type 3 is "not an inappropriate user", type
  6 has "since cleared". This is the only field in either backend carrying a
  vendor's claim about whether you may act on it.
* **mococo (TASE)** publishes ``score_sum``. TASE's own API typings define the
  per-guild ``score`` as "a number calculated on how much a user has interacted
  with this guild", and ``scoreSum`` as the sum across guilds. That is
  *interaction volume, not severity*, and no scale or threshold is published
  anywhere. It orders users; it does not grade them.
* **Detective Okappiki** publishes a boolean and a list of server names.

So the table lands where the evidence lands: **only Rotector can support a ban
on its own**, and everything else is a reason for a person to look -- with rule
R2 as the exception that lets independent corroboration carry a Rotector flag
that Rotector itself declined to conclude from.

What is deliberately not used
-----------------------------

``reason`` strings are never parsed. They are free text from three services and
they carry server names *chosen by the people being flagged*. Matching keywords
in them would be a heuristic wearing determinism's clothes, and its failure mode
-- a severe finding downgraded because its wording did not match -- is the exact
failure this table exists to avoid. Reasons are shown verbatim, and nothing else.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import IntEnum

from .verdict import (
    Signal,
    Verdict,
    flag_is_actionable,
    flag_is_suspect,
    flag_name,
)

#: the parties an Okappiki response can carry, in the order they are reported
PARTIES = ("rotector", "okappiki", "mococo")

PARTY_LABEL = {
    "rotector": "Rotector",
    "okappiki": "Detective Okappiki",
    "mococo": "mococo (TASE)",
}

APPEAL_LINKS = {
    "rotector": "https://rotector.com",
    "okappiki": "https://okappiki.com/appeal",
    "mococo": "https://moco-co.org",
}


class Recommendation(IntEnum):
    """Ordered by how much it asks of a moderator."""

    #: nothing is known about this member at all -- no database answered, or
    #: none of them has a record of the account. Not a clean result: a blank one
    NOTHING = 0
    #: every consulted database answered and none flagged the member
    NO_FINDING = 1
    #: nothing flagged, but a database that should have answered did not
    RECHECK = 2
    #: flagged, but only by findings the database itself calls informational --
    #: queued for review, since cleared, or withheld. Not an accusation
    INFORMATIONAL = 3
    #: flagged by real evidence, but nothing that supports acting on its own
    CAUTION = 4
    #: the evidence supports acting
    BAN = 5


#: recommendation -> (label, rich style, one-line meaning)
RECOMMENDATION_META: dict[Recommendation, tuple[str, str, str]] = {
    Recommendation.NOTHING: (
        "NOTHING KNOWN",
        "dim white",
        "No database has anything on this member. Not the same as nothing found.",
    ),
    Recommendation.NO_FINDING: (
        "NO FINDING",
        "green",
        "Every database answered and none flagged them. Not a clean bill of health.",
    ),
    Recommendation.RECHECK: (
        "RECHECK",
        "cyan",
        "A database did not answer, so this member has not actually been checked.",
    ),
    Recommendation.INFORMATIONAL: (
        "INFORMATIONAL",
        "cyan",
        "On record, but for something the database says is not an accusation.",
    ),
    Recommendation.CAUTION: (
        "CAUTION",
        "yellow",
        "Real evidence, but none that supports acting on its own. A person decides.",
    ),
    Recommendation.BAN: (
        "BAN SUPPORTED",
        "bold red",
        "The evidence meets the bar for acting.",
    ),
}


def recommendation_label(rec: Recommendation) -> str:
    return RECOMMENDATION_META[rec][0]


def recommendation_style(rec: Recommendation) -> str:
    return RECOMMENDATION_META[rec][1]


def recommendation_meaning(rec: Recommendation) -> str:
    return RECOMMENDATION_META[rec][2]


@dataclass(frozen=True)
class TriagePolicy:
    """The operator's own bar, separate from anything a vendor claims."""

    #: Rule R3: let Okappiki and mococo, agreeing with each other and with no
    #: Rotector flag, support a ban on their own. Off by default and
    #: deliberately so -- neither service publishes a claim that its sighting is
    #: safe to act on, so switching this on is the operator's policy call and
    #: not something either service told them to make.
    ban_on_corroborated_sighting: bool = False
    #: R3's floor on mococo's ``score_sum``. TASE publishes no scale for this
    #: number and it measures interaction volume rather than severity, so there
    #: is no correct value. 40 is what the sample records this was built against
    #: carried; it is not a vendor threshold and should be set from the
    #: operator's own observations.
    min_mococo_score: int = 40


DEFAULT_POLICY = TriagePolicy()


@dataclass(frozen=True)
class PartyFinding:
    """One database's answer about one member, or its silence."""

    party: str
    #: did this database answer at all? A database that did not answer and one
    #: that answered "not flagged" are different facts, and flattening them
    #: would let an outage read as a clean result.
    answered: bool
    flagged: bool
    #: Rotector only
    flag_type: int | None = None
    #: mococo only; interaction volume across detected guilds, not severity
    score: int | None = None
    #: the database's own words, carried through unedited and never parsed
    reason: str = ""
    #: does this database have a record of the account at all? "Rotector knows
    #: of no linked Roblox account" and "Rotector knows the account and has not
    #: flagged it" are both unflagged, and they are not the same fact -- the
    #: first supports nothing, the second is a check that came back empty.
    known: bool = True

    @property
    def label(self) -> str:
        return PARTY_LABEL.get(self.party, self.party.title())

    @property
    def material(self) -> bool:
        """Whether this flag is an accusation rather than a note.

        Rotector's types 3, 6 and 8 are flags that Rotector explicitly says are
        *not* findings against the member -- queued for review, since cleared,
        withheld. Letting those reach CAUTION would matter here in a way it does
        not elsewhere, because in this project CAUTION is the tier where acting
        becomes possible behind a second confirmation. A sighting from either
        unverified database is CAUTION-strength by construction; see
        :mod:`rsb.okappiki`.
        """
        if not self.flagged:
            return False
        if self.party == "rotector":
            return flag_is_suspect(self.flag_type)
        return True


# --------------------------------------------------------------------------
# reading a report
# --------------------------------------------------------------------------


def findings_for(report) -> list[PartyFinding]:
    """One finding per database the active backend actually consulted.

    Which databases those are is read off the report rather than configured:
    a report carrying ``signals`` came from the Okappiki backend, which asks all
    three every time, so all three are expected and a missing one is a database
    that did not answer. A report carrying only ``accounts`` came from the
    Rotector backend, which asks Rotector alone -- the other two are not silent,
    they were never consulted, and reporting them as unanswered would put every
    clean Rotector scan on RECHECK.
    """
    signals: list[Signal] = list(getattr(report, "signals", None) or [])

    if signals:
        by_source = {sig.source: sig for sig in signals}
        return [
            PartyFinding(
                party=party,
                answered=party in by_source,
                flagged=bool(by_source[party].flagged) if party in by_source else False,
                flag_type=by_source[party].flag_type if party in by_source else None,
                score=by_source[party].score if party in by_source else None,
                reason=by_source[party].reason if party in by_source else "",
            )
            for party in PARTIES
        ]

    # Rotector backend: one database, answering per Roblox account. The worst
    # account is the one the member is judged on, the same account the results
    # table and the audit-log reason already name.
    account = report.worst_account
    if account is None:
        # Rotector answered; it simply knows of no linked Roblox account. That
        # supports nothing at all, which is not the same as a check coming back
        # empty, so it is recorded as answered-but-unknown rather than clean.
        return [
            PartyFinding(
                party="rotector",
                answered=not report.error,
                flagged=False,
                known=False,
            )
        ]

    return [
        PartyFinding(
            party="rotector",
            answered=True,
            flagged=account.flag_type is not None and account.flag_type != 0,
            flag_type=account.flag_type,
            reason=", ".join(list(account.reasons)[:3]),
            known=True,
        )
    ]


# --------------------------------------------------------------------------
# the decision table
# --------------------------------------------------------------------------


@dataclass
class _Context:
    findings: list[PartyFinding]
    by_party: dict[str, PartyFinding]
    answered: list[str]
    flagged: list[str]
    #: the subset of ``flagged`` whose flag is an accusation rather than a note
    material: list[str]
    unanswered: list[str]
    #: parties other than Rotector carrying a material flag
    corroborating: list[str]
    rotector_flag: int | None
    rotector_actionable: bool
    rotector_suspect: bool
    mococo_score: int
    policy: TriagePolicy


@dataclass(frozen=True)
class Rule:
    id: str
    recommendation: Recommendation
    #: what this rule says, in one line; the settings screen renders these
    summary: str
    when: Callable[[_Context], bool]
    headline: Callable[[_Context], str]


def _names(parties: list[str]) -> str:
    labels = [PARTY_LABEL.get(p, p) for p in parties]
    if not labels:
        return "no database"
    if len(labels) == 1:
        return labels[0]
    return f"{', '.join(labels[:-1])} and {labels[-1]}"


#: The whole algorithm, as data. Evaluated in order; the first match wins.
#:
#: Read top to bottom it says: Rotector's actionable types carry a ban alone;
#: Rotector's inconclusive types carry one once an independent database saw the
#: same member; two unverified sightings carry one only if the operator has said
#: they may; anything else flagged is a person's decision; and a check that did
#: not finish is not a clean check.
RULES: tuple[Rule, ...] = (
    Rule(
        id="R0",
        recommendation=Recommendation.NOTHING,
        summary="No database answered.",
        when=lambda c: not c.answered,
        headline=lambda c: (
            "No database answered, so nothing was learned about this member. "
            "That is a failed check, not a clean one."
        ),
    ),
    Rule(
        id="R1",
        recommendation=Recommendation.BAN,
        summary=(
            "Rotector reports flag type 1 (Flagged) or 2 (Confirmed) - the types "
            "Rotector documents as safe to action."
        ),
        when=lambda c: c.rotector_actionable,
        headline=lambda c: (
            f"Rotector reports {flag_name(c.rotector_flag)} (flag type "
            f"{c.rotector_flag}), which Rotector documents as safe to action on "
            f"its own."
        ),
    ),
    Rule(
        id="R2",
        recommendation=Recommendation.BAN,
        summary=(
            "Rotector flagged them at an inconclusive type (4 Provisional, 5 "
            "Mixed) and at least one other database independently flagged them too."
        ),
        when=lambda c: (
            c.rotector_suspect and not c.rotector_actionable and bool(c.corroborating)
        ),
        headline=lambda c: (
            f"Rotector reports {flag_name(c.rotector_flag)}, which it declines to "
            f"conclude from on its own - but {_names(c.corroborating)} "
            f"independently flagged the same account. The corroboration is what "
            f"Rotector said was missing."
        ),
    ),
    Rule(
        id="R3",
        recommendation=Recommendation.BAN,
        summary=(
            "Both unverified databases flagged them and the mococo interaction "
            "score meets your threshold. Off unless you switch it on."
        ),
        when=lambda c: (
            c.policy.ban_on_corroborated_sighting
            and c.by_party["okappiki"].flagged
            and c.by_party["mococo"].flagged
            and c.mococo_score >= c.policy.min_mococo_score
        ),
        headline=lambda c: (
            f"Both Detective Okappiki and mococo flagged them, and the mococo "
            f"interaction score is {c.mococo_score}, at or above the "
            f"{c.policy.min_mococo_score} you set. Neither service publishes a "
            f"claim that its sighting is safe to act on - this bar is yours."
        ),
    ),
    Rule(
        id="R4",
        recommendation=Recommendation.CAUTION,
        summary="Flagged by real evidence, but nothing that supports acting on its own.",
        when=lambda c: bool(c.material),
        headline=lambda c: (
            f"{len(c.material)} of the {len(c.answered)} databases that answered "
            f"flagged them ({_names(c.material)}), but none of it meets the bar "
            f"for acting on its own."
        ),
    ),
    Rule(
        id="R5",
        recommendation=Recommendation.INFORMATIONAL,
        summary=(
            "Flagged only by types the database itself calls informational - "
            "queued, since cleared, or withheld. Not an accusation."
        ),
        when=lambda c: bool(c.flagged),
        headline=lambda c: (
            f"On record, but only as {flag_name(c.by_party['rotector'].flag_type)} "
            f"- which Rotector documents as not a finding against the member. "
            f"There is nothing here to act on."
        ),
    ),
    Rule(
        id="R6",
        recommendation=Recommendation.RECHECK,
        summary="Nothing flagged, but a database did not answer.",
        when=lambda c: bool(c.unanswered),
        headline=lambda c: (
            f"{_names(c.unanswered)} did not answer, so this member has not "
            f"actually been checked against every database. Run it again before "
            f"drawing anything from it."
        ),
    ),
    Rule(
        id="R7",
        recommendation=Recommendation.NOTHING,
        summary="Every database answered, and none of them has a record of the account.",
        when=lambda c: not any(f.known for f in c.findings),
        headline=lambda c: (
            f"No linked Roblox account is known to {_names(c.answered)}. There "
            f"is no finding here at all - nothing supports acting on them."
        ),
    ),
    Rule(
        id="R8",
        recommendation=Recommendation.NO_FINDING,
        summary="Every database answered and none flagged them.",
        when=lambda c: True,
        headline=lambda c: (
            "Every database answered and none of them flagged this member. That "
            "is not evidence they are safe - only that nothing has been detected "
            "yet."
        ),
    ),
)


@dataclass
class Triage:
    """What the rule table decided, and everything it decided it from."""

    recommendation: Recommendation
    #: the id of the rule that fired, e.g. "R2" -- the audit handle
    rule: str
    #: one sentence stating what was decided and on what
    headline: str
    #: the ordered facts the decision rests on, one line per database
    basis: list[str] = field(default_factory=list)
    findings: list[PartyFinding] = field(default_factory=list)
    answered: list[str] = field(default_factory=list)
    flagged: list[str] = field(default_factory=list)
    #: the databases whose flag is an accusation rather than a note
    material: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return recommendation_label(self.recommendation)

    @property
    def style(self) -> str:
        return recommendation_style(self.recommendation)

    @property
    def supports_action(self) -> bool:
        """Whether acting is supported without an override."""
        return self.recommendation is Recommendation.BAN

    @property
    def flagged_findings(self) -> list[PartyFinding]:
        return [f for f in self.findings if f.flagged]


def _basis(ctx: _Context) -> list[str]:
    lines: list[str] = []
    for finding in ctx.findings:
        if not finding.answered:
            lines.append(f"{finding.label}: did not answer.")
            continue
        if not finding.flagged:
            lines.append(f"{finding.label}: no detection.")
            continue

        if finding.party == "rotector":
            where = (
                "no flag type given"
                if finding.flag_type is None
                else f"flag type {finding.flag_type}"
            )
            if flag_is_actionable(finding.flag_type):
                stance = "Rotector documents this type as safe to action."
            elif flag_is_suspect(finding.flag_type):
                stance = "Rotector does not consider this type conclusive on its own."
            else:
                stance = "Rotector documents this type as not actionable."
            lines.append(
                f"{finding.label}: flagged - {flag_name(finding.flag_type)} "
                f"({where}). {stance}"
            )
        elif finding.party == "mococo":
            score = (
                "no interaction score given"
                if finding.score is None
                else f"interaction score {finding.score}"
            )
            lines.append(
                f"{finding.label}: flagged - {score}. TASE defines this as how "
                f"much the member interacted with the detected servers, not how "
                f"severe they are, and publishes no scale for it."
            )
        else:
            lines.append(
                f"{finding.label}: flagged. Publishes a sighting only, with no "
                f"severity or actionability claim attached."
            )
    return lines


def triage(report, policy: TriagePolicy = DEFAULT_POLICY) -> Triage:
    """Classify one member report. Never raises."""
    findings = findings_for(report)
    by_party = {f.party: f for f in findings}
    # a party the backend never consulted is absent from `findings`; asking for
    # it here must not raise, so unconsulted reads as an unanswered non-finding
    for party in PARTIES:
        by_party.setdefault(
            party, PartyFinding(party=party, answered=False, flagged=False)
        )

    flagged = [f.party for f in findings if f.flagged]
    material = [f.party for f in findings if f.material]
    rotector = by_party["rotector"]
    rotector_flag = rotector.flag_type if rotector.flagged else None

    ctx = _Context(
        findings=findings,
        by_party=by_party,
        answered=[f.party for f in findings if f.answered],
        flagged=flagged,
        material=material,
        unanswered=[f.party for f in findings if not f.answered],
        corroborating=[p for p in material if p != "rotector"],
        rotector_flag=rotector_flag,
        rotector_actionable=rotector.flagged and flag_is_actionable(rotector.flag_type),
        rotector_suspect=rotector.flagged and flag_is_suspect(rotector.flag_type),
        mococo_score=by_party["mococo"].score or 0,
        policy=policy,
    )

    rule = next(r for r in RULES if r.when(ctx))
    return Triage(
        recommendation=rule.recommendation,
        rule=rule.id,
        headline=rule.headline(ctx),
        basis=_basis(ctx),
        findings=findings,
        answered=list(ctx.answered),
        flagged=list(ctx.flagged),
        material=list(ctx.material),
    )


def policy_from_config(moderation) -> TriagePolicy:
    """Read the operator's bar off a :class:`~rsb.config.ModerationConfig`."""
    return TriagePolicy(
        ban_on_corroborated_sighting=bool(
            getattr(moderation, "ban_on_corroborated_sighting", False)
        ),
        min_mococo_score=int(getattr(moderation, "min_mococo_score", 40) or 0),
    )


def worst(triages: list[Triage]) -> Recommendation:
    """The strongest recommendation across several members, for a bulk preview."""
    if not triages:
        return Recommendation.NOTHING
    return max(t.recommendation for t in triages)


#: recommendations that mean "a database said something about this member"
FINDING_RECOMMENDATIONS = (Recommendation.CAUTION, Recommendation.BAN)


def verdict_floor(rec: Recommendation) -> Verdict:
    """The evidence tier a recommendation implies, for sorting alongside verdicts.

    Kept deliberately separate from :class:`~rsb.verdict.Verdict` itself: a
    verdict describes what a database found, a recommendation describes what a
    moderator should do about it, and the whole point of this module is that the
    two stopped being the same thing.
    """
    return {
        Recommendation.NOTHING: Verdict.UNKNOWN,
        Recommendation.NO_FINDING: Verdict.NO_DETECTIONS,
        Recommendation.RECHECK: Verdict.INFO,
        Recommendation.INFORMATIONAL: Verdict.INFO,
        Recommendation.CAUTION: Verdict.CAUTION,
        Recommendation.BAN: Verdict.THREAT,
    }[rec]
