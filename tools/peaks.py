"""Precompute the amplitude envelope each player draws its barcode from.

    python tools/peaks.py <library-dir>

The visualiser has to be driven by the audio rather than by a timer, or it is
decoration pretending to be data -- and this project's whole argument is that
the picture is made of the real thing. But neither player can analyse the
stream as it goes: ``ffplay`` runs with ``-nodisp`` and exposes nothing, and
having the browser or the app decode a second copy just to measure it would
double the bandwidth for a decoration.

So it is measured once, here, and shipped beside the music. One file per track,
fetched only when that track starts.

**The format is deliberately tiny.** One value per frame at 20 fps, quantised
to four bits and packed two per byte, then base64. A two-minute track is about
1.2 KB of payload -- small enough that the fetch is invisible next to the three
megabytes of audio it describes, and small enough that a player can hold the
whole library's worth in memory without anybody noticing.

Values are normalised to each track's own peak. Absolute loudness would make
the quiet tracks look broken next to the loud ones, and a visualiser is about
shape rather than mastering level; ``peak`` is recorded so the original scale
can be recovered if that ever matters.
"""

from __future__ import annotations

import array
import base64
import json
import subprocess
import sys
from pathlib import Path

#: frames a second. 20 is smooth enough to read as motion and coarse enough
#: that a track costs about a kilobyte.
FPS = 20
#: mono, and low: an envelope needs amplitude, not fidelity
RATE = 8000
#: 4 bits a frame, so two frames fit in a byte
LEVELS = 16

FORMAT_VERSION = 1


def envelope(path: Path) -> tuple[list[int], int, float]:
    """Peak amplitude per frame, the track's own peak, and its duration."""
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path),
         "-ac", "1", "-ar", str(RATE), "-f", "s16le", "-"],
        capture_output=True, check=True,
    ).stdout
    samples = array.array("h")
    samples.frombytes(raw[: len(raw) // 2 * 2])

    window = RATE // FPS
    frames: list[int] = []
    for start in range(0, len(samples) - window + 1, window):
        chunk = samples[start:start + window]
        # peak rather than RMS: a barcode wants transients, and max/min beats
        # abs() per sample by enough to matter across 160 tracks
        frames.append(max(max(chunk), -min(chunk)))
    peak = max(frames) if frames else 0
    return frames, peak, len(samples) / RATE


def pack(frames: list[int], peak: int) -> str:
    """Quantise to 4 bits, two frames a byte, base64."""
    if peak <= 0:
        return ""
    scaled = [min(LEVELS - 1, (value * LEVELS) // (peak + 1)) for value in frames]
    if len(scaled) % 2:
        scaled.append(0)
    packed = bytes(
        (scaled[i] << 4) | scaled[i + 1] for i in range(0, len(scaled), 2)
    )
    return base64.b64encode(packed).decode("ascii")


def analyse(path: Path) -> dict:
    frames, peak, duration = envelope(path)
    return {
        "v": FORMAT_VERSION,
        "fps": FPS,
        "levels": LEVELS,
        "duration": round(duration, 2),
        "peak": peak,
        "frames": len(frames),
        "data": pack(frames, peak),
    }


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    root = Path(argv[1]).expanduser().resolve()
    tracks = root / "tracks"
    if not tracks.is_dir():
        print(f"no tracks/ under {root}", file=sys.stderr)
        return 2

    out = root / "peaks"
    out.mkdir(exist_ok=True)
    files = sorted(tracks.glob("*.mp3"))
    total = 0
    for index, path in enumerate(files, start=1):
        target = out / f"{path.stem}.json"
        data = analyse(path)
        target.write_text(json.dumps(data, separators=(",", ":")) + "\n",
                          encoding="utf-8")
        total += target.stat().st_size
        if index % 25 == 0 or index == len(files):
            print(f"  {index}/{len(files)}  {total // 1024} KB so far")

    print(f"\n{len(files)} envelopes, {total // 1024} KB in {out}")
    print(f"average {total // max(1, len(files))} bytes a track")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
