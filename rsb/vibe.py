"""Vibe mode: music streamed from the ``music`` branch, never from the clone.

A scan of ten thousand members takes four minutes on Rotector and half an hour
on Okappiki. This is for the half hour.

The audio lives on an orphan branch rather than in ``main``, so cloning the
project downloads none of it -- a few dozen megabytes for a feature most people
will never switch on would be a few dozen megabytes on every clone, every CI
run and every update. Nothing here writes to that branch or clones it; tracks
are streamed by URL, one at a time.

``index.json`` on that branch *is* the library rather than an index of it.
GitHub serves no directory listing for raw files, so there is nothing to
enumerate: a track that is not in the manifest cannot be found, which is also
what keeps the licensing honest -- every entry carries where it came from.

Playback is ``ffplay``, which ships with FFmpeg on all three platforms and
streams a URL without downloading it first. It is looked up rather than
assumed, and its absence is a sentence rather than a traceback.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import shutil
from dataclasses import dataclass
from typing import Callable

import httpx

#: raw.githubusercontent serves a branch's files directly; no API, no token
RAW = "https://raw.githubusercontent.com/{repo}/{branch}"

MANIFEST = "index.json"
TRACK_DIR = "tracks"

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
        raise VibeError(f"could not reach the music branch: {exc}") from None
    finally:
        if owned:
            await client.aclose()

    if response.status_code == 404:
        raise VibeError(
            f"no {MANIFEST} on the {branch!r} branch of {repo}. Vibe mode reads "
            f"its library from there; see that branch's README."
        )
    if response.status_code >= 400:
        raise VibeError(f"the music branch answered HTTP {response.status_code}")

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
        self.error: str | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None
        self._order: list[int] = []
        self._at = 0

    @property
    def playing(self) -> bool:
        return self._task is not None and not self._task.done()

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
        self._at = 0

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
                f"the {self.branch!r} branch has no tracks listed yet. Add some "
                f"to {MANIFEST} there and they will show up here."
            )
            raise VibeError(self.error)
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        try:
            while True:
                if self._at >= len(self._order):
                    self._reorder()
                track = self.tracks[self._order[self._at]]
                self._at += 1
                self.current = track
                if self.on_change:
                    self.on_change(track)
                await self._play(track)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - music is never fatal
            self.error = f"vibe mode stopped: {type(exc).__name__}: {exc}"
        finally:
            self.current = None
            if self.on_change:
                self.on_change(None)

    async def _play(self, track: Track) -> None:
        binary = shutil.which("ffplay")
        if binary is None:
            raise VibeError(missing_player_reason())
        self._process = await asyncio.create_subprocess_exec(
            binary,
            "-nodisp",            # no video window; this is a terminal program
            "-autoexit",          # end the process at the end of the track
            "-hide_banner",
            "-loglevel", "error",
            "-volume", str(self.volume),
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

    async def skip(self) -> None:
        """Move to the next track by ending the current one."""
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
