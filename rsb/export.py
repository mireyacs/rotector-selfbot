"""Rendering scan results to CSV, TXT and JSON.

A long list is segmented into numbered CSV parts inside a per-export folder, so
a scan of tens of thousands of members stays openable in a spreadsheet instead
of producing one file nothing will load.

Whatever is written obeys two of Rotector's Terms of Use: every file carries
the attribution line, and every file states its 24-hour expiry, because the
terms forbid retaining responses longer than that.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .rotector import MemberReport
from .verdict import (
    ATTRIBUTION,
    category_name,
    flag_is_actionable,
    flag_name,
    source_names,
    verdict_label,
)

#: rows per CSV segment when segmentation is on
DEFAULT_SEGMENT_SIZE = 1000


@dataclass
class ExportRow:
    """One member, paired with whatever the UI knows about them."""

    discord_id: str
    username: str
    display_name: str
    report: MemberReport


# --------------------------------------------------------------------------
# columns
# --------------------------------------------------------------------------


def _worst(row: ExportRow):
    return row.report.worst_account


COLUMNS: dict[str, Callable[[ExportRow], str]] = {
    "discord_id": lambda r: r.discord_id,
    "username": lambda r: r.username,
    "display_name": lambda r: r.display_name,
    "verdict": lambda r: verdict_label(r.report.verdict),
    "flag": lambda r: flag_name(_worst(r).flag_type if _worst(r) else None),
    "actionable": lambda r: str(
        flag_is_actionable(_worst(r).flag_type if _worst(r) else None)
    ).lower(),
    "category": lambda r: category_name(_worst(r).category if _worst(r) else None) or "",
    "confidence": lambda r: (
        f"{_worst(r).confidence:.2f}"
        if _worst(r) and _worst(r).confidence is not None
        else ""
    ),
    "roblox_accounts": lambda r: " ".join(
        f"{a.username}({a.user_id})" for a in r.report.accounts
    ),
    "roblox_ids": lambda r: " ".join(str(a.user_id) for a in r.report.accounts),
    "roblox_profiles": lambda r: " ".join(a.profile_url for a in r.report.accounts),
    "detection_sources": lambda r: " ".join(
        source_names(a.sources) for a in r.report.accounts
    ),
    "tracked_servers": lambda r: str(len(r.report.servers)),
    "tracked_server_names": lambda r: " | ".join(
        s.server_name for s in r.report.servers
    ),
    "reasons": lambda r: " | ".join(
        f"{name}: {str(detail.get('message', '')).replace(chr(10), ' / ')}"
        for account in r.report.accounts
        for name, detail in (account.reasons or {}).items()
    ),
    "evidence": lambda r: " | ".join(
        str(item)
        for account in r.report.accounts
        for detail in (account.reasons or {}).values()
        for item in (detail.get("evidence") or [])
    ),
    "error": lambda r: r.report.error or "",
}

#: sensible default column set
DEFAULT_COLUMNS = [
    "discord_id",
    "username",
    "display_name",
    "verdict",
    "flag",
    "actionable",
    "category",
    "confidence",
    "roblox_accounts",
    "tracked_servers",
    "reasons",
]

ALL_COLUMNS = list(COLUMNS)


def valid_columns(names: Iterable[str]) -> list[str]:
    """Keep only columns that exist, preserving the caller's order."""
    seen, out = set(), []
    for name in names:
        if name in COLUMNS and name not in seen:
            seen.add(name)
            out.append(name)
    return out or list(DEFAULT_COLUMNS)


# --------------------------------------------------------------------------
# manifest
# --------------------------------------------------------------------------


@dataclass
class ExportManifest:
    directory: Path
    files: list[Path]
    rows: int
    segments: int
    formats: list[str]
    scope: str

    def describe(self) -> str:
        parts = f"{self.segments} segments" if self.segments > 1 else "1 file"
        return (
            f"{self.rows:,} rows -> {len(self.files)} file(s) ({parts}) "
            f"in {self.directory}"
        )


def _header_lines(guild_name: str, stamp: str, scope: str, rows: int) -> list[str]:
    expires = (
        datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        + timedelta(hours=24)
    ).strftime("%Y-%m-%d %H:%M UTC")
    return [
        f"Server:    {guild_name}",
        f"Generated: {stamp}",
        f"Scope:     {scope}",
        f"Members:   {rows}",
        f"Expires:   {expires} - Rotector Terms of Use forbid keeping this longer",
        f"Source:    {ATTRIBUTION}",
    ]


# --------------------------------------------------------------------------
# renderers
# --------------------------------------------------------------------------


def render_csv(
    rows: Sequence[ExportRow],
    directory: Path,
    stem: str,
    columns: Sequence[str],
    segment_size: int = DEFAULT_SEGMENT_SIZE,
) -> list[Path]:
    """Write CSV, split into numbered segments when the list is long.

    ``segment_size`` of 0 (or a list shorter than one segment) produces a
    single file; otherwise each part is a complete CSV with its own header, so
    every segment opens on its own.
    """
    columns = valid_columns(columns)
    written: list[Path] = []

    if segment_size and len(rows) > segment_size:
        chunks = [
            rows[i : i + segment_size] for i in range(0, len(rows), segment_size)
        ]
    else:
        chunks = [list(rows)]

    width = len(str(len(chunks)))
    for index, chunk in enumerate(chunks, start=1):
        name = (
            f"{stem}.csv"
            if len(chunks) == 1
            else f"{stem}.part{index:0{width}d}-of-{len(chunks)}.csv"
        )
        path = directory / name
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(columns)
            for row in chunk:
                writer.writerow([COLUMNS[column](row) for column in columns])
        written.append(path)
    return written


def render_txt(
    rows: Sequence[ExportRow],
    directory: Path,
    stem: str,
    columns: Sequence[str],
    guild_name: str,
    stamp: str,
    scope: str,
) -> list[Path]:
    """A readable report -- one block per member, not a table."""
    columns = valid_columns(columns)
    path = directory / f"{stem}.txt"
    labels = {c: c.replace("_", " ").title() for c in columns}
    pad = max((len(l) for l in labels.values()), default=0)

    lines: list[str] = []
    header = _header_lines(guild_name, stamp, scope, len(rows))
    rule = "=" * max(len(h) for h in header)
    lines.append(rule)
    lines.extend(header)
    lines.append(rule)
    lines.append("")

    for index, row in enumerate(rows, start=1):
        title = f"{index}. {row.display_name}"
        if row.username and row.username != row.display_name:
            title += f" (@{row.username})"
        lines.append(title)
        lines.append("-" * len(title))
        for column in columns:
            value = COLUMNS[column](row)
            if not value:
                continue
            lines.append(f"  {labels[column]:<{pad}}  {value}")
        lines.append("")

    if not rows:
        lines.append("(no members matched the selected filter)")
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    return [path]


def render_json(
    rows: Sequence[ExportRow],
    directory: Path,
    stem: str,
    columns: Sequence[str],
    guild_name: str,
    guild_id: str,
    stamp: str,
    scope: str,
) -> list[Path]:
    columns = valid_columns(columns)
    path = directory / f"{stem}.json"
    payload = {
        "guild": {"id": guild_id, "name": guild_name},
        "generated_at": stamp,
        "scope": scope,
        "expires_at": "24 hours after generated_at (Rotector Terms of Use #1)",
        "attribution": ATTRIBUTION,
        "columns": list(columns),
        "members": [
            {column: COLUMNS[column](row) for column in columns} for row in rows
        ],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return [path]


# --------------------------------------------------------------------------


def export(
    rows: Sequence[ExportRow],
    *,
    guild_name: str,
    guild_id: str,
    base_directory: Path,
    formats: Sequence[str],
    columns: Sequence[str],
    scope: str = "filtered",
    segment_size: int = DEFAULT_SEGMENT_SIZE,
    stamp: str | None = None,
) -> ExportManifest:
    """Render ``rows`` into a dedicated folder and describe what was written."""
    stamp = stamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in guild_name)[:40]
    stem = f"{safe}-{stamp}"
    directory = Path(base_directory) / stem
    directory.mkdir(parents=True, exist_ok=True)

    formats = [f.lower() for f in formats] or ["csv"]
    columns = valid_columns(columns)
    files: list[Path] = []
    segments = 1

    if "csv" in formats:
        csv_files = render_csv(rows, directory, stem, columns, segment_size)
        segments = len(csv_files)
        files += csv_files
    if "txt" in formats:
        files += render_txt(rows, directory, stem, columns, guild_name, stamp, scope)
    if "json" in formats:
        files += render_json(
            rows, directory, stem, columns, guild_name, guild_id, stamp, scope
        )

    readme = directory / "README.txt"
    readme.write_text(
        "\n".join(
            [
                *_header_lines(guild_name, stamp, scope, len(rows)),
                "",
                "Files:",
                *(f"  {f.name}" for f in files),
                "",
                "Delete this folder within 24 hours. Rotector flag statuses change,",
                "and their Terms of Use forbid retaining responses beyond that.",
                "Anyone listed here can appeal at https://rotector.com",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    files.append(readme)

    return ExportManifest(
        directory=directory,
        files=files,
        rows=len(rows),
        segments=segments,
        formats=list(formats),
        scope=scope,
    )
