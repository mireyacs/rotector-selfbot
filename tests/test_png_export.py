"""Rendering the results table to PNG."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import rsb.imagerender as imagerender
from rsb.export import DEFAULT_COLUMNS, ExportRow, export
from rsb.imagerender import (
    DEFAULT_ROWS_PER_IMAGE,
    VERDICT_COLOURS,
    available,
    render_png,
    unavailable_reason,
)
from rsb.rotector import MemberReport, RobloxAccount
from rsb.verdict import Verdict

ok = lambda m: print(f"[ok] {m}")


def make_rows(n, flag=2):
    rows = []
    for i in range(n):
        report = MemberReport(discord_id=str(1000 + i))
        report.accounts.append(
            RobloxAccount(
                user_id=4_000_000 + i, username=f"user{i:04d}_rblx",
                flag_type=flag, category=5, confidence=1.0,
                reasons={"Condo Activity": {"message": "[Trap3] entered a condo"}},
            )
        )
        rows.append(
            ExportRow(str(1000 + i), f"user{i:04d}", f"User {i:04d}", report)
        )
    return rows


def test_availability():
    assert available(), "Pillow is not installed in this environment"
    assert unavailable_reason() == ""
    ok("Pillow present, so PNG export is offered")

    # the optional dependency must degrade, not explode
    imagerender.PIL_AVAILABLE = False
    try:
        assert not available()
        assert "pip install pillow" in unavailable_reason()
        try:
            render_png([], Path(tempfile.mkdtemp()), "x", DEFAULT_COLUMNS)
            raise AssertionError("rendered without Pillow")
        except RuntimeError as exc:
            assert "Pillow" in str(exc)
        ok(f"without Pillow it explains itself: {unavailable_reason()}")
    finally:
        imagerender.PIL_AVAILABLE = True


def test_single_image():
    from PIL import Image

    out = Path(tempfile.mkdtemp())
    rows = make_rows(12)
    files = render_png(
        rows, out, "sample", DEFAULT_COLUMNS,
        title="Test Server", subtitle="filter: Threats only",
    )
    assert len(files) == 1 and files[0].name == "sample.png"
    image = Image.open(files[0])
    assert image.format == "PNG" and image.mode == "RGB"
    assert image.width > 400 and image.height > 100
    ok(f"12 rows -> one {image.width}x{image.height} PNG")


def test_segmentation():
    from PIL import Image

    out = Path(tempfile.mkdtemp())
    rows = make_rows(25)
    files = render_png(rows, out, "big", DEFAULT_COLUMNS, rows_per_image=10)
    assert len(files) == 3, [f.name for f in files]
    assert files[0].name == "big.part1-of-3.png"
    ok(f"25 rows at 10 per image -> {len(files)} parts: {[f.name for f in files]}")

    heights = [Image.open(f).height for f in files]
    assert heights[0] == heights[1] > heights[2], heights
    ok(f"full parts are equal height, the last is shorter ({heights})")

    widths = {Image.open(f).width for f in files}
    assert len(widths) == 1, widths
    ok("every part shares a width, so they stack cleanly")

    single = render_png(rows[:5], out, "small", DEFAULT_COLUMNS, rows_per_image=10)
    assert len(single) == 1 and single[0].name == "small.png"
    ok("a list shorter than one page is not split")


def test_height_tracks_rows():
    from PIL import Image

    out = Path(tempfile.mkdtemp())
    short = render_png(make_rows(5), out, "a", ["username", "verdict"])
    tall = render_png(make_rows(30), out, "b", ["username", "verdict"])
    h1, h2 = Image.open(short[0]).height, Image.open(tall[0]).height
    assert h2 > h1 * 2, (h1, h2)
    ok(f"height tracks row count ({h1}px for 5 rows, {h2}px for 30)")


def test_columns_and_width():
    from PIL import Image

    out = Path(tempfile.mkdtemp())
    rows = make_rows(6)
    narrow = render_png(rows, out, "n", ["username", "verdict"])
    wide = render_png(rows, out, "w", DEFAULT_COLUMNS)
    w1, w2 = Image.open(narrow[0]).width, Image.open(wide[0]).width
    assert w2 > w1, (w1, w2)
    ok(f"width follows the chosen columns ({w1}px for 2, {w2}px for "
       f"{len(DEFAULT_COLUMNS)})")

    # one verbose field must not stretch the image without limit
    rows[0].report.accounts[0].reasons = {
        "Condo Activity": {"message": "x" * 4000}
    }
    capped = render_png(rows, out, "c", DEFAULT_COLUMNS, max_column_chars=40)
    assert Image.open(capped[0]).width < 3000, Image.open(capped[0]).width
    ok("a 4,000-character reason is truncated rather than stretching the image")


def test_verdict_colours_appear():
    from PIL import Image

    out = Path(tempfile.mkdtemp())
    threat = render_png(make_rows(4, flag=2), out, "t", ["username", "verdict"])
    clear = render_png(make_rows(4, flag=0), out, "c", ["username", "verdict"])

    def colours(path):
        return {c for _n, c in Image.open(path).convert("RGB").getcolors(1 << 16)}

    assert VERDICT_COLOURS[Verdict.THREAT] in colours(threat[0])
    ok(f"a THREAT table contains the threat accent {VERDICT_COLOURS[Verdict.THREAT]}")
    assert VERDICT_COLOURS[Verdict.THREAT] not in colours(clear[0])
    assert VERDICT_COLOURS[Verdict.NO_DETECTIONS] in colours(clear[0])
    ok("a clear table does not, so severity is visible at a glance")


def test_through_export():
    from PIL import Image

    out = Path(tempfile.mkdtemp())
    manifest = export(
        make_rows(8),
        guild_name="My Server",
        guild_id="1",
        base_directory=out,
        formats=["csv", "png"],
        columns=DEFAULT_COLUMNS,
        scope="filter: Threats only",
        stamp="20260802T120000Z",
    )
    pngs = [f for f in manifest.files if f.suffix == ".png"]
    csvs = [f for f in manifest.files if f.suffix == ".csv"]
    assert pngs and csvs, [f.name for f in manifest.files]
    ok(f"export() writes both: {[f.name for f in csvs + pngs]}")
    assert Image.open(pngs[0]).format == "PNG"
    assert "png" in manifest.formats
    ok("the manifest reports png among its formats")

    # asking for png without Pillow drops it, keeping the rest
    imagerender.PIL_AVAILABLE = False
    try:
        manifest = export(
            make_rows(3), guild_name="S", guild_id="1", base_directory=out,
            formats=["csv", "png"], columns=DEFAULT_COLUMNS,
            stamp="20260802T130000Z",
        )
        assert not [f for f in manifest.files if f.suffix == ".png"]
        assert [f for f in manifest.files if f.suffix == ".csv"]
        assert "png" not in manifest.formats
        ok("without Pillow the PNG is dropped and the CSV still written")
    finally:
        imagerender.PIL_AVAILABLE = True


def test_empty():
    out = Path(tempfile.mkdtemp())
    files = render_png([], out, "none", DEFAULT_COLUMNS, title="Nothing")
    assert len(files) == 1 and files[0].is_file()
    ok("an empty result set still renders a headed, empty table")


test_availability()
test_single_image()
test_segmentation()
test_height_tracks_rows()
test_columns_and_width()
test_verdict_colours_appear()
test_through_export()
test_empty()
print("\nALL PNG EXPORT TESTS PASSED")
