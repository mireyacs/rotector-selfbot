"""The deterministic rule table: does it actually separate ban from caution?

The complaint this answers is that everything landed on CAUTION. Under the
Okappiki backend that was literally true -- any sighting from either unverified
database produced ``Verdict.CAUTION``, so a member seen once by one crawler and
a member all three databases agreed on read identically.

These tests pin down the separation, and pin down its limits: the table must
never promote a flag type Rotector documents as *not a finding against the
member*, however many other databases agree.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rsb.config import ModerationConfig
from rsb.moderation import BULK_SCOPES, check_eligibility, rows_for_scope
from rsb.okappiki import parse_report
from rsb.rotector import MemberReport, RobloxAccount
from rsb.triage import (
    DEFAULT_POLICY,
    RULES,
    Recommendation,
    TriagePolicy,
    policy_from_config,
    triage,
)
from rsb.verdict import Verdict, flag_is_suspect

ok = lambda m: print(f"[ok] {m}")

# --- recorded responses ---------------------------------------------------
# Both bodies came from okappiki.com/backend/api.php?action=vencord_full_check.

ALL_FLAGGED = json.loads(r"""
{"success":true,"discord_id":"1212568604604629013",
 "okappiki":{"flagged":true,"reason":"Basement Hub, prime society, Condo Links",
             "roblox_username":null,"roblox_id":null},
 "rotector":{"flagged":true,"roblox_id":9092847610,
             "username":"iskidfromgithubrepos","flag_type":2,
             "reason":"[Condo Activity] [Discord] detected in 13+ condo servers"},
 "mococo":{"flagged":true,"score_sum":40,"reason":"Detected in: Basement Hub"},
 "last_updated":"2026-08-03T08:21:58+00:00"}
""")

# the same id, re-checked live two weeks later: Rotector's flag is gone, the
# two unverified databases still see them
TWO_UNVERIFIED = json.loads(r"""
{"success":true,"discord_id":"1212568604604629013",
 "okappiki":{"flagged":true,"reason":"Basement Hub, prime society, Condo Links"},
 "rotector":{"flagged":false},
 "mococo":{"flagged":true,"score_sum":40,
           "reason":"Detected in: trlx bloodhound, ferns.club"},
 "last_updated":"2026-08-16T18:57:33+00:00"}
""")

NONE_FLAGGED = json.loads(
    '{"success":true,"discord_id":"80351110224678912",'
    '"okappiki":{"flagged":false},"rotector":{"flagged":false},'
    '"mococo":{"flagged":false},"last_updated":"2026-08-03T08:22:51+00:00"}'
)


def okappiki_body(flag_type=None, okappiki=False, mococo=False, score=40):
    """One Okappiki-backend body, with each database set independently."""
    body = {
        "success": True,
        "rotector": {"flagged": False}
        if flag_type is None
        else {"flagged": True, "flag_type": flag_type},
        "okappiki": {"flagged": okappiki},
        "mococo": {"flagged": mococo, "score_sum": score if mococo else None},
    }
    return parse_report("1212568604604629013", body)


def rotector_report(flag_type):
    """One Rotector-backend report: accounts, no signals."""
    report = MemberReport(discord_id="1212568604604629013")
    report.accounts.append(
        RobloxAccount(user_id=9092847610, username="x", flag_type=flag_type)
    )
    return report


# --- R1: Rotector's actionable types carry a ban alone ---------------------

for flag_type in (1, 2):
    result = triage(okappiki_body(flag_type=flag_type))
    assert result.recommendation is Recommendation.BAN, result
    assert result.rule == "R1", result.rule
ok("Rotector Flagged and Confirmed -> BAN by R1, with no corroboration needed")

assert triage(parse_report("1", ALL_FLAGGED)).rule == "R1"
ok("the recorded three-database response resolves to R1 on Rotector's own flag")

# --- R2: the separation the old rule could not make ------------------------
# This is the whole point. Provisional and Mixed are findings Rotector declines
# to conclude from; an independent sighting supplies what Rotector said was
# missing. Before the table, both of these were indistinguishable CAUTION.

for flag_type in (4, 5):
    alone = triage(okappiki_body(flag_type=flag_type))
    assert alone.recommendation is Recommendation.CAUTION, f"{flag_type} alone"
    assert alone.rule == "R4"

    corroborated = triage(okappiki_body(flag_type=flag_type, okappiki=True))
    assert corroborated.recommendation is Recommendation.BAN, f"{flag_type} + one"
    assert corroborated.rule == "R2"
ok("Rotector Provisional/Mixed: CAUTION alone, BAN by R2 once another database agrees")

# --- the limit on R2 -------------------------------------------------------
# Rotector documents type 3 as "not an inappropriate user" and type 6 as having
# "since cleared". Those are not weak accusations, they are not accusations, and
# corroboration must not launder them into one.

for flag_type in (3, 6, 8):
    result = triage(okappiki_body(flag_type=flag_type, okappiki=True, mococo=True))
    assert result.recommendation is not Recommendation.BAN, (
        f"flag type {flag_type} must never reach BAN"
    )
    assert not flag_is_suspect(flag_type)
ok("Rotector types 3, 6 and 8 never reach BAN, however many databases agree")

# and they must not reach CAUTION either -- in this project CAUTION is the tier
# where acting becomes possible behind a second confirmation
informational = triage(okappiki_body(flag_type=3))
assert informational.recommendation is Recommendation.INFORMATIONAL
assert informational.rule == "R5"
ok("an informational flag type alone is INFORMATIONAL, not CAUTION")

# a type added after this was written could as easily mean "cleared"
assert triage(okappiki_body(flag_type=7, okappiki=True)).recommendation is not Recommendation.BAN
ok("an unrecognised flag type does not escalate")

# --- R3: two unverified sightings, opt-in only -----------------------------

default = triage(parse_report("1", TWO_UNVERIFIED))
assert default.recommendation is Recommendation.CAUTION, default
assert default.rule == "R4"
assert DEFAULT_POLICY.ban_on_corroborated_sighting is False
ok("Okappiki + mococo with no Rotector flag -> CAUTION under the default policy")

opted = TriagePolicy(ban_on_corroborated_sighting=True, min_mococo_score=40)
assert triage(parse_report("1", TWO_UNVERIFIED), opted).rule == "R3"
assert "this bar is yours" in triage(parse_report("1", TWO_UNVERIFIED), opted).headline
ok("the same body -> BAN by R3 once the operator opts in and the score clears their bar")

strict = TriagePolicy(ban_on_corroborated_sighting=True, min_mococo_score=41)
assert triage(parse_report("1", TWO_UNVERIFIED), strict).recommendation is Recommendation.CAUTION
ok("R3 respects its threshold: a score of 40 against a bar of 41 stays CAUTION")

one_only = triage(okappiki_body(okappiki=True), opted)
assert one_only.recommendation is Recommendation.CAUTION
ok("R3 needs both unverified databases, not one")

# --- not-flagged is not one outcome but three ------------------------------

clean = triage(parse_report("1", NONE_FLAGGED))
assert clean.recommendation is Recommendation.NO_FINDING and clean.rule == "R8"
assert "not evidence they are safe" in clean.headline
ok("all three answered, none flagged -> NO_FINDING, explicitly not 'safe'")

partial = triage(parse_report("1", {"success": True, "okappiki": {"flagged": False}}))
assert partial.recommendation is Recommendation.RECHECK and partial.rule == "R6"
assert partial.answered == ["okappiki"]
ok("a database that did not answer -> RECHECK, not clean")

nothing = triage(MemberReport(discord_id="1"))
assert nothing.recommendation is Recommendation.NOTHING and nothing.rule == "R7"
assert "No linked Roblox account is known to Rotector" in nothing.headline
ok("no linked account known -> NOTHING KNOWN, kept distinct from a clean check")

# --- the Rotector backend flows through the same table ---------------------
# It consults one database, so the other two are not silent -- they were never
# asked. Reporting them as unanswered would put every clean scan on RECHECK.

assert triage(rotector_report(2)).rule == "R1"
assert triage(rotector_report(4)).recommendation is Recommendation.CAUTION
assert triage(rotector_report(3)).recommendation is Recommendation.INFORMATIONAL
solo_clean = triage(rotector_report(0))
assert solo_clean.recommendation is Recommendation.NO_FINDING, solo_clean
assert solo_clean.rule == "R8", "an unconsulted database is not an unanswered one"
ok("the Rotector backend uses the same table without tripping RECHECK")

# --- eligibility: the decision the table actually drives -------------------

allowed = check_eligibility(triage_report := parse_report("1", ALL_FLAGGED))
assert allowed.allowed and not allowed.needs_override
assert allowed.explanation.startswith("[R1]"), allowed.explanation
ok("an R1 finding is actionable without an override, and cites the rule")

caution = check_eligibility(parse_report("1", TWO_UNVERIFIED))
assert caution.allowed and caution.needs_override and caution.needs_double_confirm
ok("a CAUTION finding stays behind a second, separate confirmation")

assert not check_eligibility(parse_report("1", TWO_UNVERIFIED), allow_caution=False).allowed
ok("allow_caution=False still blocks the whole CAUTION tier")

# the promotion is visible where it matters: same member, different decision
promoted = check_eligibility(okappiki_body(flag_type=4, mococo=True))
assert promoted.allowed and not promoted.needs_override
assert not check_eligibility(okappiki_body(flag_type=4)).explanation.startswith("[R2]")
ok("corroboration turns a double-confirm CAUTION into a plain, citable BAN")

# informational and empty findings are blocked when a threat is required
for report in (okappiki_body(flag_type=3), MemberReport(discord_id="1")):
    assert not check_eligibility(report).allowed
ok("informational and empty findings remain blocked under require_threat")

# --- bulk scope ------------------------------------------------------------


class _Row:
    def __init__(self, report):
        self.report = report
        self.actioned = False


rows = [
    _Row(okappiki_body(flag_type=2)),  # R1  ban
    _Row(okappiki_body(flag_type=4, mococo=True)),  # R2  ban
    _Row(okappiki_body(okappiki=True)),  # R4  caution
    _Row(okappiki_body(flag_type=3)),  # R5  informational
    _Row(parse_report("1", NONE_FLAGGED)),  # R8  clean
]

ban_scope = rows_for_scope("ban", [], rows)
assert len(ban_scope) == 2, [r.report.verdict for r in ban_scope]
threat_scope = rows_for_scope("threat", [], rows)
assert len(threat_scope) == 1, "verdict THREAT is only the one Rotector concluded on"
ok("the 'ban' scope is a different, larger set than the 'threat' scope -- by one R2")

assert len(rows_for_scope("caution", [], rows)) == 3
assert "ban" in dict(BULK_SCOPES)
ok("the scope list offers the rule table's answer alongside the raw verdicts")

# --- policy plumbing -------------------------------------------------------

config = ModerationConfig()
assert policy_from_config(config) == DEFAULT_POLICY
config.ban_on_corroborated_sighting = True
config.min_mococo_score = 55
assert policy_from_config(config) == TriagePolicy(True, 55)
ok("the operator's bar is read off ModerationConfig, defaults matching the module's")

# --- properties of the table itself ----------------------------------------

assert [r.id for r in RULES] == ["R0", "R1", "R2", "R3", "R4", "R5", "R6", "R7", "R8"]
assert all(r.summary for r in RULES), "every rule documents itself"
ok("the rule table is ordered, complete and self-describing")

bodies = [ALL_FLAGGED, TWO_UNVERIFIED, NONE_FLAGGED, {"success": True}, {}]
reports = [parse_report("1", b) for b in bodies] + [
    MemberReport(discord_id="1"),
    MemberReport(discord_id="1", error="boom"),
    rotector_report(1),
]
for report in reports:
    assert triage(report, opted).rule, "a rule must always fire"
ok("the table is total -- every report lands on exactly one rule")

for report in reports:
    first = triage(report, opted)
    for _ in range(5):
        again = triage(report, opted)
        assert (again.recommendation, again.rule, again.basis) == (
            first.recommendation,
            first.rule,
            first.basis,
        )
ok("repeated classification of the same report is identical, every time")

# the basis explains every database and never dresses the TASE score as severity
basis = triage(parse_report("1", ALL_FLAGGED)).basis
assert len(basis) == 3
assert any("publishes no scale" in line for line in basis)
assert any("safe to action" in line for line in basis)
ok("the basis cites all three databases and qualifies the mococo score")

# a recommendation is not a verdict, and the module keeps them apart
assert triage(parse_report("1", ALL_FLAGGED)).recommendation is Recommendation.BAN
assert parse_report("1", ALL_FLAGGED).verdict is Verdict.THREAT
assert triage(okappiki_body(flag_type=4, mococo=True)).recommendation is Recommendation.BAN
assert okappiki_body(flag_type=4, mococo=True).verdict is Verdict.CAUTION
ok("evidence tier and recommendation stay separate: an R2 ban still reads CAUTION as evidence")

print("\nall triage checks passed")
