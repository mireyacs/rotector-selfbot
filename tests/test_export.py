"""Export rendering, config persistence, and moderation reason construction.

No network: everything here is pure logic over synthetic reports.
"""
import csv
import json
import sys
import tempfile
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rsb.config import Config, write_section
from rsb.export import (
    ALL_COLUMNS,
    DEFAULT_COLUMNS,
    ExportRow,
    export,
    render_csv,
    valid_columns,
)
from rsb.moderation import build_reason, check_eligibility, summarise_finding
from rsb.rotector import MemberReport, RobloxAccount, TrackedServer
from rsb.verdict import Verdict

ok = lambda m: print(f"[ok] {m}")


def make_row(i, flag=None, reasons=None, servers=0):
    report = MemberReport(discord_id=str(i))
    if flag is not None:
        report.accounts.append(
            RobloxAccount(
                user_id=1000 + i,
                username=f"rblx{i}",
                flag_type=flag,
                category=5,
                confidence=1.0,
                reasons=reasons or {},
            )
        )
    for n in range(servers):
        report.servers.append(
            TrackedServer(f"s{n}", f"Server {n}", None, None, False, False)
        )
    return ExportRow(
        discord_id=str(i),
        username=f"user{i}",
        display_name=f"User {i}",
        report=report,
    )


def test_columns():
    assert valid_columns(["discord_id", "nope", "verdict"]) == ["discord_id", "verdict"]
    assert valid_columns([]) == list(DEFAULT_COLUMNS)
    assert valid_columns(["a", "b"]) == list(DEFAULT_COLUMNS), "unknown-only falls back"
    assert valid_columns(["verdict", "verdict"]) == ["verdict"], "deduped"
    ok(f"column validation: {len(ALL_COLUMNS)} available, "
       f"{len(DEFAULT_COLUMNS)} on by default")


def test_csv_segmentation():
    tmp = Path(tempfile.mkdtemp())
    rows = [make_row(i, flag=2) for i in range(2500)]

    single = render_csv(rows, tmp, "one", DEFAULT_COLUMNS, segment_size=0)
    assert len(single) == 1
    ok("segment_size=0 writes a single file however long the list is")

    parts = render_csv(rows, tmp, "many", DEFAULT_COLUMNS, segment_size=1000)
    assert len(parts) == 3, [p.name for p in parts]
    assert parts[0].name == "many.part1-of-3.csv", parts[0].name
    ok(f"2,500 rows split into {len(parts)} segments: {[p.name for p in parts]}")

    total = 0
    for part in parts:
        with part.open(newline="", encoding="utf-8") as handle:
            reader = list(csv.reader(handle))
        # every segment must be a complete CSV in its own right
        assert reader[0] == list(DEFAULT_COLUMNS), f"{part.name} missing header"
        total += len(reader) - 1
    assert total == len(rows), f"{total} rows across segments, expected {len(rows)}"
    ok(f"every segment carries its own header; {total:,} rows preserved in total")

    short = render_csv(rows[:10], tmp, "short", DEFAULT_COLUMNS, segment_size=1000)
    assert len(short) == 1 and short[0].name == "short.csv"
    ok("a list shorter than one segment is not split")


def test_full_export():
    tmp = Path(tempfile.mkdtemp())
    reasons = {"Condo Activity": {"message": "[Trap3] entered a condo",
                                  "evidence": ["Game: x", "Game: y"]}}
    rows = [make_row(i, flag=2, reasons=reasons, servers=2) for i in range(120)]

    manifest = export(
        rows,
        guild_name="My / Server",
        guild_id="111",
        base_directory=tmp,
        formats=["csv", "txt", "json"],
        columns=DEFAULT_COLUMNS,
        scope="filter: Threats only",
        segment_size=50,
        stamp="20260801T120000Z",
    )

    assert manifest.directory.is_dir()
    assert manifest.directory.name.startswith("My---Server-"), manifest.directory.name
    ok(f"export folder created: {manifest.directory.name}/ (unsafe chars stripped)")

    names = sorted(f.name for f in manifest.files)
    assert any(n.endswith(".txt") for n in names)
    assert any(n.endswith(".json") for n in names)
    assert sum(1 for n in names if n.endswith(".csv")) == 3
    assert "README.txt" in names
    ok(f"wrote {len(names)} files: {names}")

    payload = json.loads(
        next(f for f in manifest.files if f.suffix == ".json").read_text(encoding="utf-8")
    )
    assert len(payload["members"]) == 120
    assert payload["attribution"] and "rotector.com" in payload["attribution"]
    assert "24 hours" in payload["expires_at"]
    ok("JSON carries every member, the attribution, and the 24h expiry")

    text = next(f for f in manifest.files if f.suffix == ".txt").read_text(encoding="utf-8")
    assert "User 0" in text and "Condo Activity" in text
    assert "rotector.com" in text
    assert "Scope:     filter: Threats only" in text, "scope not recorded"
    ok("TXT report is readable, records the scope, and carries attribution")

    readme = (manifest.directory / "README.txt").read_text(encoding="utf-8")
    assert "Delete this folder within 24 hours" in readme
    assert "appeal" in readme.lower()
    ok("README states the retention limit and the appeal route")


def test_export_respects_columns():
    tmp = Path(tempfile.mkdtemp())
    rows = [make_row(0, flag=1)]
    manifest = export(
        rows,
        guild_name="G",
        guild_id="1",
        base_directory=tmp,
        formats=["csv"],
        columns=["discord_id", "verdict"],
        stamp="20260801T120000Z",
    )
    csv_file = next(f for f in manifest.files if f.suffix == ".csv")
    header = next(csv.reader(csv_file.open(newline="", encoding="utf-8")))
    assert header == ["discord_id", "verdict"], header
    ok(f"only the chosen columns are written: {header}")


def test_config_round_trip():
    tmp = Path(tempfile.mkdtemp())
    path = tmp / "config.toml"
    path.write_text(
        '# leading comment\n'
        '[discord]\n'
        'token = "abc"   # inline comment\n'
        '\n'
        '[export]\n'
        'formats = ["json"]\n'
        '\n'
        '[scan]\n'
        'skip_bots = true\n',
        encoding="utf-8",
    )

    cfg = Config.load(path)
    assert cfg.export.formats == ["json"]
    cfg.export.formats = ["csv", "txt"]
    cfg.export.segment_size = 250
    cfg.export.columns = ["discord_id", "verdict"]
    saved = cfg.save_export_settings()
    assert saved == path

    body = path.read_text(encoding="utf-8")
    assert "# leading comment" in body and "# inline comment" in body
    ok("saving from the UI preserves comments elsewhere in the file")

    reparsed = tomllib.loads(body)
    assert reparsed["export"]["formats"] == ["csv", "txt"]
    assert reparsed["export"]["segment_size"] == 250
    assert reparsed["scan"]["skip_bots"] is True, "other sections damaged"
    assert reparsed["discord"]["token"] == "abc"
    ok("settings round-trip and neighbouring sections survive intact")

    again = Config.load(path)
    assert again.export.formats == ["csv", "txt"] and again.export.segment_size == 250
    ok("a fresh load sees the remembered defaults")

    missing = tmp / "new" / "config.toml"
    write_section(missing, "export", {"formats": ["csv"]})
    assert tomllib.loads(missing.read_text(encoding="utf-8"))["export"]["formats"] == ["csv"]
    ok("writing to a config file that does not exist yet creates it")


def test_moderation_reasons():
    reasons = {"Condo Activity": {"message": "entered a condo game"}}
    confirmed = make_row(1, flag=2, reasons=reasons).report

    summary = summarise_finding(confirmed)
    assert "Confirmed" in summary and "Condo" in summary
    ok(f"finding summarised: {summary!r}")

    auto = build_reason(confirmed)
    assert "Confirmed" in auto and "rotector.com" in auto
    ok(f"automatic reason: {auto!r}")

    custom = build_reason(confirmed, custom="Predatory behaviour toward minors")
    assert custom.startswith("Predatory behaviour")
    assert "rotector.com" in custom, "attribution missing from a custom reason"
    ok(f"custom reason still carries the appeal link: {custom!r}")

    already = build_reason(confirmed, custom="See rotector.com for details")
    assert already.count("rotector.com") == 1, "attribution duplicated"
    ok("attribution is not duplicated when the operator already included it")

    long = build_reason(confirmed, custom="x" * 900)
    assert len(long) <= 512, len(long)
    assert long.endswith("Appeal at rotector.com"), "appeal link trimmed away"
    ok(f"over-long reasons trim the body, never the appeal link ({len(long)} chars)")

    braces = build_reason(confirmed, template="Banned {unknown_field}")
    assert braces and "rotector.com" in braces
    ok("a malformed reason template degrades instead of raising")


def test_moderation_eligibility():
    actionable = check_eligibility(make_row(1, flag=2).report)
    assert actionable.allowed and not actionable.needs_override
    ok(f"Confirmed is actionable: {actionable.explanation}")

    flagged = check_eligibility(make_row(2, flag=1).report)
    assert flagged.allowed and not flagged.needs_override
    ok("Flagged is actionable")

    for flag in (0, 3, 6, 8):
        result = check_eligibility(make_row(3, flag=flag).report)
        assert result.needs_override, f"flag {flag} should need an override"
        assert not result.allowed, f"flag {flag} allowed under require_threat"
        assert not result.needs_double_confirm
    ok("flag types 0/3/6/8 all require an explicit override, per the docs")

    # 4 (Provisional) and 5 (Mixed) are CAUTION: permitted, but only through a
    # second confirmation. Not the same as actionable, and not the same as
    # blocked either.
    for flag in (4, 5):
        caution = check_eligibility(make_row(3, flag=flag).report)
        assert caution.allowed and caution.needs_double_confirm, flag
        assert not check_eligibility(
            make_row(3, flag=flag).report, allow_caution=False
        ).allowed
    ok("flag types 4/5 (CAUTION) are permitted behind a second confirmation, "
       "and blocked again by allow_caution=false")

    unknown = check_eligibility(make_row(4).report)
    assert unknown.needs_override and "no Roblox account" in unknown.explanation
    ok(f"UNKNOWN explains there is no finding at all: {unknown.explanation[:60]}...")

    relaxed = check_eligibility(make_row(5, flag=0).report, require_threat=False)
    assert relaxed.allowed and relaxed.needs_override
    ok("require_threat=false permits the action but still flags it as unsupported")


test_columns()
test_csv_segmentation()
test_full_export()
test_export_respects_columns()
print()
test_config_round_trip()
print()
test_moderation_reasons()
test_moderation_eligibility()
print("\nALL EXPORT TESTS PASSED")
