"""Vibe mode: music streamed from a separate repository, never from the clone.

A scan of ten thousand members takes four minutes on Rotector and half an hour
on Okappiki. This is for the half hour.

The library lives in its own repository (``mireyacs/openlofi``) rather than on
a branch of this one. An orphan branch looked like it would keep the audio out
of the download and does not: a plain ``git clone`` fetches every branch, so
half a gigabyte of music would have landed in every clone of a two-megabyte
program, permanently, since history cannot be trimmed without a rewrite.
Nothing here clones or writes to it; tracks are streamed by URL, one at a
time.

``index.json`` there *is* the library rather than an index of it.
GitHub serves no directory listing for raw files, so there is nothing to
enumerate: a track that is not in the manifest cannot be found, which is also
what keeps the licensing honest -- every entry carries where it came from.

Playback is ``ffplay``, which ships with FFmpeg on all three platforms and
streams a URL without downloading it first. It is looked up rather than
assumed, and its absence is a sentence rather than a traceback.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
import random
import shutil
from dataclasses import dataclass, field
from typing import Callable

import httpx

#: raw.githubusercontent serves a branch's files directly; no API, no token
RAW = "https://raw.githubusercontent.com/{repo}/{branch}"

MANIFEST = "index.json"
TRACK_DIR = "tracks"
#: the envelopes tools/peaks.py writes, one per track
PEAK_DIR = "peaks"

#: a manifest is a small JSON file; anything larger is not one
FETCH_TIMEOUT = 15.0


class VibeError(RuntimeError):
    pass


@dataclass(frozen=True)
class Track:
    """One entry from the manifest.

    ``license`` and ``source`` are carried through to the UI rather than
    dropped: royalty-free is not the same as free of obligations, and most of
    the licences this music will come under want attribution shown.
    """

    file: str
    title: str
    artist: str = ""
    license: str = ""
    source: str = ""
    duration: int | None = None

    @property
    def label(self) -> str:
        return f"{self.title} - {self.artist}" if self.artist else self.title

    def credit(self) -> str:
        bits = [self.label]
        if self.license:
            bits.append(self.license)
        return "  ".join(bits)

    def url(self, repo: str, branch: str) -> str:
        base = RAW.format(repo=repo, branch=branch)
        return f"{base}/{TRACK_DIR}/{self.file}"

    def peaks_url(self, repo: str, branch: str) -> str:
        base = RAW.format(repo=repo, branch=branch)
        stem = self.file.rsplit(".", 1)[0]
        return f"{base}/{PEAK_DIR}/{stem}.json"

    @classmethod
    def parse(cls, raw: dict) -> "Track | None":
        name = str(raw.get("file") or "").strip()
        if not name or "/" in name or name.startswith("."):
            # the manifest names a file inside tracks/, not a path out of it
            return None
        duration = raw.get("duration")
        return cls(
            file=name,
            title=str(raw.get("title") or name).strip(),
            artist=str(raw.get("artist") or "").strip(),
            license=str(raw.get("license") or "").strip(),
            source=str(raw.get("source") or "").strip(),
            duration=int(duration) if isinstance(duration, (int, float)) else None,
        )


@dataclass
class Envelope:
    """The amplitude of one track over time, as the visualiser reads it.

    Unpacked from the 4-bit packing described in the library's README. Missing
    or malformed data is not an error -- the player falls back to a flat line
    and keeps playing, because a visualiser is the least important thing on
    screen.
    """

    fps: int = 20
    levels: int = 16
    values: list[int] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return len(self.values) / self.fps if self.fps else 0.0

    def at(self, seconds: float) -> float:
        """Level at a moment, 0..1."""
        if not self.values:
            return 0.0
        index = int(seconds * self.fps)
        index = max(0, min(len(self.values) - 1, index))
        return self.values[index] / (self.levels - 1)

    def window(self, seconds: float, bars: int, span: float) -> list[float]:
        """``bars`` levels ending at ``seconds``, spanning ``span`` seconds.

        The barcode scrolls: the right-hand bar is now and the left-hand one is
        ``span`` ago, so the field reads as the track running past rather than
        as a meter twitching in place.
        """
        if bars <= 0:
            return []
        step = span / bars
        return [self.at(seconds - (bars - 1 - i) * step) for i in range(bars)]

    @classmethod
    def parse(cls, body: dict) -> "Envelope":
        try:
            fps = int(body.get("fps") or 20)
            levels = int(body.get("levels") or 16)
            raw = base64.b64decode(str(body.get("data") or ""), validate=True)
        except Exception:  # noqa: BLE001 - a bad envelope is not a broken track
            return cls()
        values: list[int] = []
        for byte in raw:
            values.append(byte >> 4)
            values.append(byte & 0x0F)
        frames = body.get("frames")
        if isinstance(frames, int) and 0 < frames <= len(values):
            values = values[:frames]   # the packing pads odd lengths by one
        return cls(fps=fps, levels=levels, values=values)


async def fetch_envelope(track: Track, repo: str, branch: str, client=None
                         ) -> Envelope:
    """The track's envelope, or an empty one. Never raises."""
    owned = client is None
    client = client or httpx.AsyncClient(timeout=FETCH_TIMEOUT)
    try:
        response = await client.get(track.peaks_url(repo, branch))
        if response.status_code >= 400:
            return Envelope()
        return Envelope.parse(response.json())
    except Exception:  # noqa: BLE001 - the music matters, the picture does not
        return Envelope()
    finally:
        if owned:
            await client.aclose()


def player_available() -> bool:
    """True when ffplay can be found on PATH."""
    return shutil.which("ffplay") is not None


def missing_player_reason() -> str:
    return (
        "ffplay was not found on PATH, so vibe mode has nothing to play with. "
        "It ships with FFmpeg: 'sudo apt install ffmpeg', 'brew install ffmpeg', "
        "or 'winget install Gyan.FFmpeg'."
    )


def manifest_url(repo: str, branch: str) -> str:
    return f"{RAW.format(repo=repo, branch=branch)}/{MANIFEST}"


async def fetch_tracks(repo: str, branch: str, client=None) -> list[Track]:
    """Read the library. Raises :class:`VibeError` with something readable."""
    url = manifest_url(repo, branch)
    owned = client is None
    client = client or httpx.AsyncClient(timeout=FETCH_TIMEOUT)
    try:
        response = await client.get(url)
    except httpx.HTTPError as exc:
        raise VibeError(f"could not reach the music library: {exc}") from None
    finally:
        if owned:
            await client.aclose()

    if response.status_code == 404:
        raise VibeError(
            f"no {MANIFEST} at {repo}@{branch}. Vibe mode reads its library "
            f"from there; see that repository's README."
        )
    if response.status_code >= 400:
        raise VibeError(f"{repo} answered HTTP {response.status_code}")

    try:
        body = response.json()
    except ValueError:
        raise VibeError(f"{MANIFEST} is not valid JSON") from None

    entries = body.get("tracks") if isinstance(body, dict) else None
    if not isinstance(entries, list):
        raise VibeError(f"{MANIFEST} has no 'tracks' list")

    tracks = [t for t in (Track.parse(e) for e in entries if isinstance(e, dict)) if t]
    return tracks


class Vibe:
    """Plays the library, one track at a time, in the background.

    Deliberately one process at a time and deliberately not a mixer: it starts
    ffplay on a URL, waits for it to exit, and starts the next one. Stopping
    kills the process rather than fading, because the alternative is holding a
    socket open on somebody's machine after they asked for quiet.
    """

    def __init__(
        self,
        repo: str,
        branch: str,
        volume: int = 70,
        shuffle: bool = True,
        on_change: Callable[["Track | None"], None] | None = None,
    ) -> None:
        self.repo = repo
        self.branch = branch
        self.volume = max(0, min(100, volume))
        self.shuffle = shuffle
        self.on_change = on_change

        self.tracks: list[Track] = []
        self.current: Track | None = None
        self.envelope: Envelope = Envelope()
        self.error: str | None = None
        #: where the current ffplay was told to start, and when it started.
        #: ffplay has no control channel, so seeking is relaunching it with
        #: -ss; the offset has to be tracked here or position would reset.
        self._offset = 0.0
        self._started_at = 0.0
        self._seek_to: float | None = None
        self._step = 0
        self._process: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None
        self._order: list[int] = []
        self._at = 0

    @property
    def playing(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def position(self) -> float:
        """Seconds into the current track."""
        if self.current is None or not self._started_at:
            return 0.0
        return self._offset + max(0.0, time.monotonic() - self._started_at)

    @property
    def duration(self) -> float:
        if self.current is not None and self.current.duration:
            return float(self.current.duration)
        return self.envelope.duration

    def bars(self, count: int, span: float = 6.0) -> list[float]:
        """The visualiser's levels, newest on the right."""
        return self.envelope.window(self.position, count, span)

    def describe(self) -> str:
        if self.error:
            return self.error
        if not self.playing:
            return "Vibe mode off."
        if self.current is None:
            return "Vibe mode starting..."
        return f"Vibe: {self.current.credit()}"

    # -- library -----------------------------------------------------------

    async def load(self, client=None) -> list[Track]:
        self.tracks = await fetch_tracks(self.repo, self.branch, client)
        self._reorder()
        return self.tracks

    def _reorder(self) -> None:
        self._order = list(range(len(self.tracks)))
        if self.shuffle:
            random.shuffle(self._order)
        # -1 because _run advances before it plays
        self._at = -1

    # -- playing -----------------------------------------------------------

    async def start(self, client=None) -> None:
        """Load the library if needed and play until stopped."""
        if self.playing:
            return
        if not player_available():
            self.error = missing_player_reason()
            raise VibeError(self.error)
        self.error = None
        if not self.tracks:
            await self.load(client)
        if not self.tracks:
            self.error = (
                f"{self.repo} lists no tracks yet. Add some to {MANIFEST} "
                f"there and they will show up here."
            )
            raise VibeError(self.error)
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        try:
            while True:
                if self._seek_to is None:
                    # a seek replays the same track from an offset, so the
                    # step only applies when we are actually moving on
                    self._at += self._step if self._step else 1
                    self._step = 0
                    if self._at >= len(self._order):
                        self._reorder()
                    if self._at < 0:
                        self._at = len(self._order) - 1
                    track = self.tracks[self._order[self._at]]
                    if track is not self.current:
                        self.envelope = Envelope()
                    self.current = track
                    self._offset = 0.0
                    if self.on_change:
                        self.on_change(track)
                    asyncio.create_task(self._load_envelope(track))
                else:
                    track = self.current
                    self._offset = self._seek_to
                    self._seek_to = None

                await self._play(track, self._offset)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - music is never fatal
            self.error = f"vibe mode stopped: {type(exc).__name__}: {exc}"
        finally:
            self.current = None
            if self.on_change:
                self.on_change(None)

    async def _load_envelope(self, track: Track) -> None:
        envelope = await fetch_envelope(track, self.repo, self.branch)
        if track is self.current:
            self.envelope = envelope

    async def _play(self, track: Track, offset: float = 0.0) -> None:
        binary = shutil.which("ffplay")
        if binary is None:
            raise VibeError(missing_player_reason())
        self._started_at = time.monotonic()
        self._process = await asyncio.create_subprocess_exec(
            binary,
            "-nodisp",            # no video window; this is a terminal program
            "-autoexit",          # end the process at the end of the track
            "-hide_banner",
            "-loglevel", "error",
            "-volume", str(self.volume),
            # ffplay offers no control channel, so seeking means starting it
            # again from the offset. Before -i, which is the fast form.
            "-ss", f"{max(0.0, offset):.2f}",
            track.url(self.repo, self.branch),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env={**os.environ, "SDL_AUDIODRIVER": os.environ.get("SDL_AUDIODRIVER", "")}
            if os.environ.get("SDL_AUDIODRIVER") else None,
        )
        try:
            await self._process.wait()
        finally:
            self._process = None

    async def skip(self, step: int = 1) -> None:
        """Move a track. ``step`` of -1 goes back."""
        self._step = step
        self._seek_to = None
        await self._interrupt()

    async def previous(self) -> None:
        """Back to the start of this track, or to the one before it.

        The rule every music player has: pressing back a few seconds in means
        "start this again", and only near the beginning does it mean "the one
        before".
        """
        if self.position > 3.0:
            await self.seek(0.0)
        else:
            await self.skip(-1)

    async def seek(self, seconds: float) -> None:
        """Jump to a position by restarting ffplay there."""
        if self.current is None:
            return
        limit = self.duration or 0.0
        target = max(0.0, min(seconds, limit - 1.0) if limit else seconds)
        self._seek_to = target
        self._step = 0
        await self._interrupt()

    async def nudge(self, seconds: float) -> None:
        await self.seek(self.position + seconds)

    async def _interrupt(self) -> None:
        process = self._process
        if process is not None and process.returncode is None:
            process.kill()

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
        process = self._process
        if process is not None and process.returncode is None:
            process.kill()
            try:
                await process.wait()
            except Exception:  # noqa: BLE001 - it is going away regardless
                pass
        self._process = None
        self.current = None
        if task is not None:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
