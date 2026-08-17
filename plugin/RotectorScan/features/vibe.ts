/*
 * RotectorScan — a Discord-client port of rotector-selfbot
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * Vibe mode: music streamed from a separate repository, never from the plugin.
 *
 * A scan of ten thousand members takes four minutes on Rotector and half an
 * hour on Okappiki. This is for the half hour, and it is the same library the
 * terminal app plays and the project page plays: `mireyacs/openlofi`, whose
 * `index.json` *is* the library rather than an index of it. GitHub serves no
 * directory listing for raw files, so a track that is not in the manifest
 * cannot be found — which is also what keeps the licensing honest, because
 * every entry carries where it came from and under what licence.
 *
 * Three things about this port are not preferences.
 *
 * **The CSP trap.** `media-src` cannot be granted through
 * `VencordNative.csp.requestAddOverride`: that API only accepts `connect-src`,
 * `img-src`, `style-src` and `font-src`. Equicord's wildcard covers `media-src`
 * so an `<audio src="https://raw.githubusercontent.com/…">` would work there and
 * fail silently on Vencord, with no way for the user to fix it. So the audio is
 * *fetched* — which is `connect-src`, the one directive that can be granted —
 * and played from a `blob:` URL, and `blob:` is already permitted in every
 * directive on both clients. Every object URL is revoked when its track goes.
 *
 * **The picture is drawn, never typed.** The visualiser is an `AnalyserNode`
 * into a `<canvas>`: bars in `currentColor`, no glyphs, no images, nothing that
 * moves position. It is gated twice — on the plugin's `motion` setting and on
 * `prefers-reduced-motion` — and when the gate is shut the field is painted once
 * as a still barcode rather than animated at a lower rate.
 *
 * **Music is never fatal.** A missing manifest, a 404 on a track, a browser that
 * refuses to autoplay, a peaks file that will not parse: each of those is a
 * sentence in the player and nothing else. Nothing here may throw into a scan.
 *
 * The Python module (rsb/vibe.py) shells out to ffplay because a terminal
 * program has no audio device of its own; a renderer does, so `<audio>` replaces
 * the subprocess and seeking becomes `currentTime` instead of relaunching the
 * process with `-ss`. Everything else — the manifest shape, the 4-bit envelope
 * packing, the shuffle order, the "back near the start means the previous track"
 * rule — is carried across unchanged.
 */

import { settings } from "../settings";

/** raw.githubusercontent serves a branch's files directly; no API, no token */
export const RAW_HOST = "https://raw.githubusercontent.com";

export const MANIFEST = "index.json";
export const TRACK_DIR = "tracks";
/** the envelopes tools/peaks.py writes, one per track */
export const PEAK_DIR = "peaks";

/** a manifest is a small JSON file; anything larger is not one */
export const FETCH_TIMEOUT_MS = 15_000;
/** a track is a few megabytes over somebody else's CDN, so it gets longer */
export const TRACK_TIMEOUT_MS = 60_000;

export const DEFAULT_REPO = "mireyacs/openlofi";
export const DEFAULT_BRANCH = "main";

/** seconds of audio the field spans, left to right — the page's own value */
export const FIELD_SPAN = 8;

/**
 * The fixed barcode the field lights.
 *
 * Which columns are bars never changes; how bright they are is the audio. That
 * is the difference between the project's device and a chart drawn in bars, and
 * it is why these numbers are here rather than derived from the spectrum.
 */
export const FIELD_PERIOD = 24;
export const FIELD_BARS: ReadonlyArray<readonly [number, number]> = [
    [0, 1], [3, 1], [9, 2], [14, 1], [21, 1],
];

export class VibeError extends Error {
    constructor(message: string) {
        super(message);
        this.name = "VibeError";
    }
}

// --------------------------------------------------------------------------
// the library
// --------------------------------------------------------------------------

/**
 * One entry from the manifest.
 *
 * `license` and `source` are carried through to the UI rather than dropped:
 * royalty-free is not the same as free of obligations, and most of the licences
 * this music comes under want attribution shown.
 */
export interface Track {
    /** the file name inside tracks/, never a path */
    file: string;
    title: string;
    url: string;
    peaksUrl?: string;
    artist: string;
    license: string;
    source: string;
    duration?: number;
}

/** "title — artist", or just the title. */
export function trackLabel(track: Track | null | undefined): string {
    if (!track) return "";
    return track.artist ? `${track.title} — ${track.artist}` : track.title;
}

/** What has to be shown for the track to be played legitimately. */
export function trackCredit(track: Track | null | undefined): string {
    if (!track) return "";
    const bits = [trackLabel(track)];
    if (track.license) bits.push(track.license);
    return bits.join("  ·  ");
}

/**
 * `https://raw.githubusercontent.com/{repo}/{branch}`.
 *
 * The repo is `owner/name`, so its slash is structural and each segment is
 * encoded separately — `encodeURIComponent` on the whole thing would turn the
 * separator into `%2F` and produce a URL that 404s for a reason nobody could
 * see by reading it.
 */
export function rawBase(repo: string, branch: string): string {
    const segments = String(repo || DEFAULT_REPO)
        .split("/")
        .map(part => part.trim())
        .filter(Boolean)
        .map(encodeURIComponent);
    const ref = encodeURIComponent(String(branch || DEFAULT_BRANCH).trim() || DEFAULT_BRANCH);
    return `${RAW_HOST}/${segments.join("/")}/${ref}`;
}

export function manifestUrl(repo: string, branch: string): string {
    return `${rawBase(repo, branch)}/${MANIFEST}`;
}

/**
 * One manifest entry, or null.
 *
 * The manifest names a file *inside* `tracks/`, not a path out of it, so an
 * entry carrying a slash or a leading dot is dropped rather than resolved. That
 * check is in the Python too and it is the only thing standing between a
 * third-party manifest and a request to a URL of its choosing.
 */
export function parseTrack(raw: any, repo: string, branch: string): Track | null {
    if (!raw || typeof raw !== "object") return null;

    const file = String(raw.file ?? "").trim();
    if (!file || file.includes("/") || file.includes("\\") || file.startsWith(".")) return null;

    const base = rawBase(repo, branch);
    const stem = file.replace(/\.[^.]+$/, "");
    const duration = Number(raw.duration);

    return {
        file,
        title: String(raw.title ?? "").trim() || file,
        artist: String(raw.artist ?? "").trim(),
        license: String(raw.license ?? "").trim(),
        source: String(raw.source ?? "").trim(),
        duration: Number.isFinite(duration) && duration > 0 ? duration : undefined,
        url: `${base}/${TRACK_DIR}/${encodeURIComponent(file)}`,
        peaksUrl: `${base}/${PEAK_DIR}/${encodeURIComponent(stem)}.json`,
    };
}

/** Read the library. Throws `VibeError` with something a person can act on. */
export async function fetchTracks(repo: string, branch: string): Promise<Track[]> {
    const url = manifestUrl(repo, branch);
    const name = `${repo || DEFAULT_REPO}@${branch || DEFAULT_BRANCH}`;

    let response: Response;
    try {
        response = await timedFetch(url, FETCH_TIMEOUT_MS, { cache: "no-cache" });
    } catch (err: any) {
        // A TypeError here means the request never left the browser, which on
        // Vencord is what a missing connect-src permission looks like. Naming
        // the host is the only way the reader can connect that to the button
        // the player offers.
        throw new VibeError(
            `Could not reach the music library at ${RAW_HOST}: ` +
            `${err?.message || String(err)}. If this client blocks the host, ` +
            "vibe mode needs connect-src permission for it."
        );
    }

    if (response.status === 404) {
        throw new VibeError(
            `No ${MANIFEST} at ${name}. Vibe mode reads its library from there; ` +
            "see that repository's README."
        );
    }
    if (!response.ok) {
        throw new VibeError(`${name} answered HTTP ${response.status}.`);
    }

    let body: any;
    try {
        body = await response.json();
    } catch {
        throw new VibeError(`${MANIFEST} at ${name} is not valid JSON.`);
    }

    const entries = body && typeof body === "object" ? body.tracks : null;
    if (!Array.isArray(entries)) {
        throw new VibeError(`${MANIFEST} at ${name} has no 'tracks' list.`);
    }

    const tracks: Track[] = [];
    for (const entry of entries) {
        const track = parseTrack(entry, repo, branch);
        if (track) tracks.push(track);
    }
    return tracks;
}

// --------------------------------------------------------------------------
// the envelope
// --------------------------------------------------------------------------

/**
 * The amplitude of one track over time, as the visualiser reads it when there
 * is no live analyser to read instead.
 *
 * Unpacked from the 4-bit packing the library's README describes: one byte is
 * two frames, high nibble first. Missing or malformed data is not an error —
 * the field falls back to a flat line and the music keeps playing, because a
 * visualiser is the least important thing on screen.
 */
export interface Envelope {
    fps: number;
    levels: number;
    values: Uint8Array;
}

export const EMPTY_ENVELOPE: Envelope = { fps: 20, levels: 16, values: new Uint8Array(0) };

export function parseEnvelope(body: any): Envelope {
    if (!body || typeof body !== "object") return EMPTY_ENVELOPE;

    const fps = Number(body.fps);
    const levels = Number(body.levels);
    const data = typeof body.data === "string" ? body.data : "";
    if (!data) return EMPTY_ENVELOPE;

    let binary: string;
    try {
        binary = atob(data);
    } catch {
        return EMPTY_ENVELOPE;
    }

    let values = new Uint8Array(binary.length * 2);
    for (let i = 0; i < binary.length; i++) {
        const byte = binary.charCodeAt(i) & 0xff;
        values[i * 2] = byte >> 4;
        values[i * 2 + 1] = byte & 0x0f;
    }

    // the packing pads an odd frame count by one, and `frames` is how many of
    // them are real
    const frames = Number(body.frames);
    if (Number.isFinite(frames) && frames > 0 && frames <= values.length) {
        values = values.subarray(0, frames);
    }

    return {
        fps: Number.isFinite(fps) && fps > 0 ? fps : 20,
        levels: Number.isFinite(levels) && levels > 1 ? levels : 16,
        values,
    };
}

/** Level at a moment, 0..1. */
export function envelopeAt(envelope: Envelope | null | undefined, seconds: number): number {
    if (!envelope || envelope.values.length === 0) return 0;
    const fps = envelope.fps > 0 ? envelope.fps : 20;
    let index = Math.round((Number.isFinite(seconds) ? seconds : 0) * fps);
    if (index < 0) index = 0;
    if (index >= envelope.values.length) index = envelope.values.length - 1;
    const span = envelope.levels > 1 ? envelope.levels - 1 : 15;
    return Math.max(0, Math.min(1, envelope.values[index] / span));
}

export function envelopeDuration(envelope: Envelope | null | undefined): number {
    if (!envelope || !envelope.fps) return 0;
    return envelope.values.length / envelope.fps;
}

/** The track's envelope, or an empty one. Never throws. */
export async function fetchEnvelope(track: Track | null | undefined): Promise<Envelope> {
    if (!track?.peaksUrl) return EMPTY_ENVELOPE;
    try {
        const response = await timedFetch(track.peaksUrl, FETCH_TIMEOUT_MS, { cache: "force-cache" });
        if (!response.ok) return EMPTY_ENVELOPE;
        return parseEnvelope(await response.json());
    } catch {
        // the music matters, the picture does not
        return EMPTY_ENVELOPE;
    }
}

// --------------------------------------------------------------------------
// settings, read defensively
// --------------------------------------------------------------------------

/**
 * The stored settings, or an empty object.
 *
 * `settings.store` throws until the plugin is registered, and this module may
 * be imported from settings.ts itself (an `onChange` that pokes the player),
 * which under a circular import would leave `settings` momentarily undefined.
 * Both cases fall through to the Vencord settings object the plugin's values
 * actually live in, and then to nothing at all.
 */
function stored(): Record<string, any> {
    try {
        const store = (settings as any)?.store;
        if (store) return store as Record<string, any>;
    } catch {
        // not registered yet
    }
    try {
        return (globalThis as any)?.Vencord?.Settings?.plugins?.RotectorScan ?? {};
    } catch {
        return {};
    }
}

function num(value: unknown, fallback: number, min: number, max: number): number {
    const parsed = typeof value === "number" ? value : Number(value);
    if (!Number.isFinite(parsed)) return fallback;
    return Math.min(max, Math.max(min, parsed));
}

function bool(value: unknown, fallback: boolean): boolean {
    return typeof value === "boolean" ? value : fallback;
}

function text(value: unknown, fallback: string): string {
    const parsed = typeof value === "string" ? value.trim() : "";
    return parsed || fallback;
}

/** Whether vibe mode exists at all. Off by default: this is a music player in somebody's chat client. */
export function vibeEnabled(): boolean {
    return bool(stored().vibeEnabled, false);
}

export function vibeRepo(): string {
    return text(stored().vibeRepo, DEFAULT_REPO);
}

export function vibeBranch(): string {
    return text(stored().vibeBranch, DEFAULT_BRANCH);
}

export function vibeVolume(): number {
    return num(stored().vibeVolume, 60, 0, 100);
}

export function vibeShuffle(): boolean {
    return bool(stored().vibeShuffle, true);
}

export function vibeFollowScans(): boolean {
    return bool(stored().vibeFollowScans, false);
}

/**
 * Whether the field is allowed to animate.
 *
 * Two gates, and the system one wins: the plugin's `motion` setting is off by
 * default because adding drift to somebody else's client is a larger imposition
 * than adding it to your own page, and `prefers-reduced-motion` is a person
 * telling their whole computer to stop moving. A player that ignored either
 * would be a player that animates in the one place it was told not to.
 */
export function motionAllowed(): boolean {
    if (!bool(stored().motion, false)) return false;
    try {
        return !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    } catch {
        return true;
    }
}

// --------------------------------------------------------------------------
// the player
// --------------------------------------------------------------------------

export type VibeStatus = "idle" | "loading" | "ready" | "playing" | "paused" | "error";

export interface VibeState {
    status: VibeStatus;
    /** what is loaded or playing right now */
    track: Track | null;
    /** how many tracks the manifest listed */
    library: number;
    position: number;
    duration: number;
    /** 0..100, as the setting stores it */
    volume: number;
    shuffle: boolean;
    /** a sentence, shown verbatim; empty when nothing is wrong */
    error: string;
    /** the browser refused to start audio without a click */
    blocked: boolean;
    /** a track is being fetched into a blob */
    fetching: boolean;
}

const IDLE_STATE: VibeState = {
    status: "idle",
    track: null,
    library: 0,
    position: 0,
    duration: 0,
    volume: 60,
    shuffle: true,
    error: "",
    blocked: false,
    fetching: false,
};

/**
 * What the field is showing at this instant.
 *
 * Read straight off the audio element rather than out of `state`, because the
 * canvas paints at frame rate and `timeupdate` fires four times a second — a
 * seek bar driven by the state is fine, a barcode driven by it stutters.
 */
export interface VibeSample {
    level: number;
    position: number;
    duration: number;
    playing: boolean;
}

/**
 * Plays the library, one track at a time.
 *
 * Deliberately one track at a time and deliberately not a mixer, exactly as the
 * Python is: it fetches a track, plays it, and fetches the next one when that
 * ends. Stopping releases the blob rather than fading, because the alternative
 * is holding a few megabytes of somebody else's music in memory after they
 * asked for quiet.
 */
export class VibePlayer {
    private current: VibeState = IDLE_STATE;
    private listeners = new Set<() => void>();

    private audio: HTMLAudioElement | null = null;
    private objectUrl: string | null = null;

    private tracks: Track[] = [];
    private order: number[] = [];
    private at = -1;

    /** the manifest, fetched once per repo@branch and reused */
    private manifest: Promise<Track[]> | null = null;
    private manifestKey = "";

    /** every track load carries one, so a slow fetch cannot overwrite a newer one */
    private token = 0;
    private fetching: AbortController | null = null;

    private audioCtx: any = null;
    private analyser: any = null;
    private mediaSource: any = null;
    private spectrum: Uint8Array | null = null;

    private envelope: Envelope = EMPTY_ENVELOPE;
    private appliedVolume = -1;
    /** consecutive tracks this client could not decode; see the error handler */
    private decodeFailures = 0;

    get state(): VibeState {
        return this.current;
    }

    /**
     * Take the volume and shuffle settings into the state.
     *
     * Called when a surface mounts, because the settings screen is somewhere
     * else entirely and nothing tells the player when a value there changed.
     * Everything else is read at the moment it is used.
     */
    sync(): void {
        const volume = vibeVolume();
        const shuffle = vibeShuffle();
        if (volume !== this.current.volume || shuffle !== this.current.shuffle) {
            this.emit({ volume, shuffle });
        }
        this.applyVolume();
    }

    subscribe(fn: () => void): () => void {
        if (typeof fn !== "function") return () => { };
        this.listeners.add(fn);
        return () => {
            this.listeners.delete(fn);
        };
    }

    /** The tracks the manifest listed, in manifest order. Empty until loaded. */
    library(): Track[] {
        return this.tracks.slice();
    }

    /**
     * Load the manifest.
     *
     * Shared between callers so pressing Play three times fetches it once, and
     * keyed on repo@branch so changing either in settings picks up the new
     * library without a reload.
     */
    load(force?: boolean): Promise<Track[]> {
        const key = `${vibeRepo()}@${vibeBranch()}`;
        if (force || key !== this.manifestKey) {
            this.manifest = null;
            this.manifestKey = key;
        }
        if (this.manifest) return this.manifest;

        this.emit({ status: this.current.track ? this.current.status : "loading", error: "" });

        const repo = vibeRepo();
        const branch = vibeBranch();
        const pending = fetchTracks(repo, branch)
            .then(tracks => {
                this.tracks = tracks;
                this.reorder();
                if (tracks.length === 0) {
                    this.emit({
                        status: "error",
                        library: 0,
                        error:
                            `${repo} lists no tracks yet. Add some to ${MANIFEST} there ` +
                            "and they will show up here.",
                    });
                } else {
                    this.emit({
                        library: tracks.length,
                        status: this.current.status === "loading" ? "ready" : this.current.status,
                    });
                }
                return tracks;
            })
            .catch(err => {
                // A failed manifest is not cached: the next press of Play should
                // try again rather than repeat a stale sentence about a network
                // that has since come back.
                this.manifest = null;
                this.tracks = [];
                this.order = [];
                this.at = -1;
                this.emit({
                    status: "error",
                    library: 0,
                    error: err?.message ? String(err.message) : String(err),
                });
                return [] as Track[];
            });

        this.manifest = pending;
        return pending;
    }

    /**
     * Start playing: a given track, the paused one, or the next one.
     *
     * Returns nothing, on purpose. Every caller is a click handler and none of
     * them can do anything useful with a rejected promise — the failure belongs
     * in the player's own error line, where the user is already looking.
     */
    play(track?: Track): void {
        void this.begin(track).catch(err => {
            this.emit({ status: "error", fetching: false, error: describeError(err) });
        });
    }

    pause(): void {
        try {
            this.audio?.pause();
        } catch {
            // a detached element; nothing to pause
        }
        if (this.current.status === "playing") this.emit({ status: "paused" });
    }

    /** Play if paused, pause if playing. What the one button does. */
    toggle(): void {
        if (this.current.status === "playing") this.pause();
        else this.play();
    }

    next(): void {
        this.step(1);
    }

    /**
     * Back to the start of this track, or to the one before it.
     *
     * The rule every music player has: pressing back a few seconds in means
     * "start this again", and only near the beginning does it mean "the one
     * before".
     */
    prev(): void {
        if (this.livePosition() > 3) {
            this.seek(0);
            return;
        }
        this.step(-1);
    }

    /**
     * Stop, and let go of the audio.
     *
     * The blob is revoked here rather than kept for a possible resume: it is a
     * few megabytes of somebody else's music, held in a chat client, after the
     * user said stop.
     */
    stop(): void {
        this.token++;
        this.abortFetch();
        try {
            this.audio?.pause();
            if (this.audio) this.audio.removeAttribute("src");
            this.audio?.load();
        } catch {
            // the element is going quiet regardless
        }
        this.releaseUrl();
        this.envelope = EMPTY_ENVELOPE;
        this.emit({
            status: this.tracks.length ? "ready" : "idle",
            position: 0,
            fetching: false,
            blocked: false,
        });
    }

    seek(seconds: number): void {
        const audio = this.audio;
        if (!audio) return;
        const duration = this.liveDuration();
        let target = Number.isFinite(seconds) ? seconds : 0;
        if (target < 0) target = 0;
        if (duration > 0 && target > duration - 0.25) target = Math.max(0, duration - 0.25);
        try {
            audio.currentTime = target;
        } catch {
            // seeking before metadata arrives; the position is about to be 0 anyway
            return;
        }
        this.emit({ position: target });
    }

    nudge(seconds: number): void {
        this.seek(this.livePosition() + (Number.isFinite(seconds) ? seconds : 0));
    }

    /** Volume is 0..100, the way the setting stores it and the way ffplay took it. */
    setVolume(value: number): void {
        const volume = num(value, 60, 0, 100);
        this.appliedVolume = volume;
        if (this.audio) {
            try {
                this.audio.volume = volume / 100;
            } catch {
                // out of range on a hostile element; nothing to do
            }
        }
        try {
            const store = stored();
            if (store && typeof store === "object") store.vibeVolume = volume;
        } catch {
            // settings are not writable yet; the live volume is set regardless
        }
        this.emit({ volume });
    }

    setShuffle(value: boolean): void {
        const shuffle = Boolean(value);
        try {
            const store = stored();
            if (store && typeof store === "object") store.vibeShuffle = shuffle;
        } catch {
            // as above
        }
        this.reorder(shuffle);
        this.emit({ shuffle });
    }

    /**
     * The visualiser's reading, at frame rate.
     *
     * The level comes from the `AnalyserNode` when one exists and from the
     * shipped envelope when it does not — the same ~2 KB file the terminal
     * player and the project page read, so the picture degrades to the same
     * picture rather than to a flat line.
     */
    sample(): VibeSample {
        return {
            level: this.level(),
            position: this.livePosition(),
            duration: this.liveDuration(),
            playing: this.current.status === "playing",
        };
    }

    /**
     * The level `seconds` ago, for the scrolling field.
     *
     * Only the envelope can answer this: an analyser knows now and nothing else.
     * When there is no envelope the caller's own history buffer is the answer,
     * which is why this returns -1 rather than 0 — a real silence and "I cannot
     * say" are different, and drawing them the same way would paint a bar field
     * that goes dark whenever the peaks file is missing.
     */
    levelAgo(seconds: number): number {
        if (this.envelope.values.length === 0) return -1;
        return envelopeAt(this.envelope, this.livePosition() - Math.max(0, seconds));
    }

    /** A sentence describing what the player is doing. Mirrors Vibe.describe(). */
    describe(): string {
        const state = this.current;
        if (state.error) return state.error;
        if (state.status === "idle") return "Vibe mode off.";
        if (state.status === "loading") return "Vibe mode starting…";
        if (!state.track) return "Nothing playing.";
        if (state.status === "playing") return `Vibe: ${trackCredit(state.track)}`;
        return trackCredit(state.track);
    }

    // -- internals ---------------------------------------------------------

    private emit(patch: Partial<VibeState>): void {
        this.current = { ...this.current, ...patch };
        for (const listener of Array.from(this.listeners)) {
            try {
                listener();
            } catch (err) {
                // one bad subscriber must not stop the rest of the UI updating
                console.error("[RotectorScan] vibe subscriber failed", err);
            }
        }
    }

    private async begin(track?: Track): Promise<void> {
        const audio = this.element();
        if (!audio) {
            this.emit({
                status: "error",
                error: "This client has no <audio> element to play with, so vibe mode cannot run.",
            });
            return;
        }

        if (!this.tracks.length) {
            await this.load();
            if (!this.tracks.length) return;   // load() has already said why
        }

        if (track) {
            const index = this.tracks.indexOf(track);
            const place = index >= 0 ? this.order.indexOf(index) : -1;
            if (place >= 0) this.at = place;
            await this.start(track);
            return;
        }

        // Resuming: the element still holds the blob of a paused track, so this
        // is the one path that does not fetch anything.
        if (this.current.track && this.objectUrl && audio.src) {
            await this.resume();
            return;
        }

        // Stopped rather than paused: the blob was released, but pressing play
        // after stop means "that one again" and not "skip it".
        if (this.current.track && this.current.status === "ready") {
            await this.start(this.current.track);
            return;
        }

        await this.start(this.advance(1));
    }

    private step(direction: number): void {
        if (!this.tracks.length) {
            this.play();
            return;
        }
        const track = this.advance(direction);
        void this.start(track).catch(err => {
            this.emit({ status: "error", fetching: false, error: describeError(err) });
        });
    }

    /** Move `direction` places through the shuffled order and return the track. */
    private advance(direction: number): Track {
        if (!this.order.length) this.reorder();
        this.at += direction || 1;
        if (this.at >= this.order.length) {
            // Round the end of the library: reshuffled, and starting at the top
            // of the new order rather than wherever the old index landed.
            this.reorder();
            this.at = 0;
        }
        if (this.at < 0) this.at = this.order.length - 1;
        const index = this.order[this.at] ?? 0;
        return this.tracks[index];
    }

    private reorder(shuffle?: boolean): void {
        this.order = this.tracks.map((_, index) => index);
        const wanted = typeof shuffle === "boolean" ? shuffle : vibeShuffle();
        if (wanted) {
            for (let i = this.order.length - 1; i > 0; i--) {
                const j = Math.floor(Math.random() * (i + 1));
                const swap = this.order[i];
                this.order[i] = this.order[j];
                this.order[j] = swap;
            }
        }
        // -1 because advance() moves before it plays, exactly as _run does in
        // rsb/vibe.py — a fresh order starts at its first entry, not its second.
        this.at = -1;
    }

    /**
     * Fetch a track into a blob and play it.
     *
     * The fetch is the whole point of the exercise: `connect-src` is grantable
     * and `media-src` is not, so the audio arrives as bytes and is handed to the
     * element as a `blob:` URL, which every client already permits.
     */
    private async start(track: Track | null | undefined): Promise<void> {
        if (!track) return;
        const audio = this.element();
        if (!audio) return;

        const token = ++this.token;
        this.abortFetch();
        this.envelope = EMPTY_ENVELOPE;
        this.emit({
            track,
            status: "loading",
            fetching: true,
            position: 0,
            duration: track.duration ?? 0,
            error: "",
            blocked: false,
        });

        // The picture is fetched alongside the audio and is never waited on.
        void fetchEnvelope(track).then(envelope => {
            if (this.token === token) this.envelope = envelope;
        });

        const controller = newController();
        this.fetching = controller;

        let url: string;
        try {
            const response = await timedFetch(track.url, TRACK_TIMEOUT_MS, {
                cache: "force-cache",
                signal: controller?.signal,
            });
            if (!response.ok) {
                throw new VibeError(
                    `${track.file} answered HTTP ${response.status}. The manifest lists it, ` +
                    "but the file is not there."
                );
            }
            const blob = await response.blob();
            if (this.token !== token) return;   // superseded while downloading
            url = URL.createObjectURL(blob);
        } catch (err: any) {
            if (this.token !== token) return;
            if (err?.name === "AbortError") return;
            this.emit({
                status: "error",
                fetching: false,
                error:
                    `Could not fetch ${track.file}: ${describeError(err)} ` +
                    `Vibe mode fetches audio over connect-src and plays it from a blob, ` +
                    `because media-src cannot be granted in this client.`,
            });
            return;
        } finally {
            if (this.fetching === controller) this.fetching = null;
        }

        this.releaseUrl();
        this.objectUrl = url;

        try {
            audio.src = url;
            audio.load();
        } catch (err) {
            this.emit({ status: "error", fetching: false, error: describeError(err) });
            return;
        }

        this.emit({ fetching: false });
        await this.resume();
    }

    /** Start or restart the element, and say plainly when the browser refuses. */
    private async resume(): Promise<void> {
        const audio = this.audio;
        if (!audio) return;

        this.applyVolume();
        this.attachGraph();

        try {
            const started = audio.play();
            if (started && typeof started.then === "function") await started;
            this.decodeFailures = 0;
            this.emit({ status: "playing", blocked: false, error: "" });
        } catch (err: any) {
            if (err?.name === "NotAllowedError") {
                // Autoplay policy. This is the one failure the user can fix in
                // one click, so it is a state rather than an error string.
                this.emit({
                    status: "paused",
                    blocked: true,
                    error: "Your client would not start audio on its own. Press play.",
                });
                return;
            }
            if (err?.name === "NotSupportedError") {
                // The element rejected the blob rather than failing later, so
                // the "error" listener never fires; this is the same failure and
                // gets the same bounded skip.
                this.decodeFailures++;
                const name = this.current.track?.file || "that track";
                this.releaseUrl();
                if (this.decodeFailures > 3) {
                    this.emit({
                        status: "error",
                        error:
                            `This client cannot play ${name}, or the three before it. Vibe ` +
                            "mode has stopped rather than keep downloading tracks it cannot play.",
                    });
                    return;
                }
                this.emit({
                    status: "error",
                    error: `This client cannot play ${name}. Skipping to the next track.`,
                });
                this.step(1);
                return;
            }
            this.emit({ status: "error", error: describeError(err) });
        }
    }

    /** The one audio element, created on demand. */
    private element(): HTMLAudioElement | null {
        if (this.audio) return this.audio;
        let audio: HTMLAudioElement;
        try {
            audio = new Audio();
        } catch {
            return null;
        }

        audio.preload = "auto";
        // The source is a blob from our own fetch, so there is no cross-origin
        // media here at all and nothing for an AnalyserNode to be tainted by.
        audio.crossOrigin = "anonymous";

        audio.addEventListener("timeupdate", () => {
            this.applyVolume();
            this.emit({ position: this.livePosition(), duration: this.liveDuration() });
        });
        audio.addEventListener("durationchange", () => {
            this.emit({ duration: this.liveDuration() });
        });
        audio.addEventListener("play", () => {
            if (this.current.status !== "playing") this.emit({ status: "playing", blocked: false });
        });
        audio.addEventListener("pause", () => {
            if (this.current.status === "playing") this.emit({ status: "paused" });
        });
        audio.addEventListener("ended", () => {
            // The blob for the finished track goes now rather than when the next
            // one has downloaded, which is the contract's "revoke each object URL
            // when the track ends" and also the difference between holding one
            // track in memory and holding two.
            this.releaseUrl();
            this.step(1);
        });
        audio.addEventListener("error", () => {
            if (!this.objectUrl) return;   // a deliberate stop, not a failure
            const name = this.current.track?.file || "that track";
            this.decodeFailures++;
            if (this.decodeFailures > 3) {
                // Three in a row is not a bad file, it is a client that cannot
                // play this library at all — skipping again would spend the rest
                // of the session downloading tracks it will never decode.
                this.stop();
                this.emit({
                    status: "error",
                    error:
                        `This client could not decode ${name}, or the three before it. ` +
                        "Vibe mode has stopped rather than keep downloading tracks it " +
                        "cannot play.",
                });
                return;
            }
            this.emit({
                status: "error",
                fetching: false,
                error: `${name} could not be decoded. Skipping to the next track.`,
            });
            this.releaseUrl();
            this.step(1);
        });

        this.audio = audio;
        return audio;
    }

    private applyVolume(): void {
        const wanted = vibeVolume();
        if (wanted === this.appliedVolume) return;
        this.appliedVolume = wanted;
        try {
            if (this.audio) this.audio.volume = wanted / 100;
        } catch {
            // as above
        }
        if (this.current.volume !== wanted) this.emit({ volume: wanted });
    }

    /**
     * Route the element through an AnalyserNode, once.
     *
     * Only built when the field is allowed to animate: with motion off nothing
     * reads the analyser, and putting the audio through a graph it does not need
     * is a way to make somebody's music silent for no benefit if any part of the
     * Web Audio API misbehaves. `createMediaElementSource` can only be called
     * once per element, and after it the element's output goes *through* the
     * graph — so if anything below it fails, the source is wired straight to the
     * destination rather than left dangling.
     */
    private attachGraph(): void {
        if (this.analyser || !motionAllowed()) {
            // A context created in an earlier gesture can be suspended by the
            // browser when tabs change; resuming is cheap and idempotent.
            try {
                if (this.audioCtx?.state === "suspended") void this.audioCtx.resume();
            } catch {
                // nothing to resume
            }
            return;
        }
        const audio = this.audio;
        if (!audio) return;

        const Ctor = (globalThis as any).AudioContext || (globalThis as any).webkitAudioContext;
        if (typeof Ctor !== "function") return;

        try {
            const ctx = this.audioCtx || new Ctor();
            this.audioCtx = ctx;
            if (!this.mediaSource) this.mediaSource = ctx.createMediaElementSource(audio);
            try {
                const analyser = ctx.createAnalyser();
                analyser.fftSize = 128;
                analyser.smoothingTimeConstant = 0.7;
                this.mediaSource.connect(analyser);
                analyser.connect(ctx.destination);
                this.analyser = analyser;
                this.spectrum = new Uint8Array(analyser.frequencyBinCount);
            } catch {
                // The graph is optional; the sound is not.
                this.mediaSource.connect(ctx.destination);
                this.analyser = null;
            }
            if (ctx.state === "suspended") void ctx.resume();
        } catch {
            // No Web Audio here. The envelope fallback covers the field.
            this.analyser = null;
        }
    }

    private level(): number {
        const analyser = this.analyser;
        const bins = this.spectrum;
        if (analyser && bins) {
            try {
                analyser.getByteFrequencyData(bins);
                // The bottom two thirds of the spectrum: this music lives there,
                // and the top bins are mostly the codec's noise floor, which
                // would light every bar to the same dull grey.
                const upTo = Math.max(1, Math.floor(bins.length * 0.66));
                let total = 0;
                for (let i = 0; i < upTo; i++) total += bins[i];
                return Math.max(0, Math.min(1, total / upTo / 255));
            } catch {
                // fall through to the envelope
            }
        }
        if (this.current.status !== "playing") return 0;
        return envelopeAt(this.envelope, this.livePosition());
    }

    private livePosition(): number {
        const value = Number(this.audio?.currentTime);
        return Number.isFinite(value) && value > 0 ? value : 0;
    }

    private liveDuration(): number {
        const value = Number(this.audio?.duration);
        if (Number.isFinite(value) && value > 0) return value;
        const declared = this.current.track?.duration;
        if (typeof declared === "number" && declared > 0) return declared;
        return envelopeDuration(this.envelope);
    }

    private abortFetch(): void {
        const controller = this.fetching;
        this.fetching = null;
        if (!controller) return;
        try {
            controller.abort();
        } catch {
            // already gone
        }
    }

    private releaseUrl(): void {
        const url = this.objectUrl;
        this.objectUrl = null;
        if (!url) return;
        try {
            URL.revokeObjectURL(url);
        } catch {
            // the URL is going away with the page regardless
        }
    }
}

export const vibePlayer = new VibePlayer();

/**
 * The scan queue told us it is busy, or that it has gone quiet.
 *
 * Wired from the job queue rather than reached for from here: this module must
 * not import the scanner, because the player has to be safe to load when no
 * scan machinery exists at all. Nothing happens unless the user asked for both
 * vibe mode and `vibeFollowScans`.
 */
let scanWasActive = false;

export function noteScanActivity(active: boolean): void {
    try {
        // The caller is a queue subscriber and fires on every progress tick, so
        // only the edges do anything: calling stop() a hundred times a second
        // would emit a hundred state changes to every mounted player.
        const wanted = Boolean(active);
        if (wanted === scanWasActive) return;
        scanWasActive = wanted;

        if (!vibeEnabled() || !vibeFollowScans()) return;
        if (active) {
            if (vibePlayer.state.status !== "playing") vibePlayer.play();
        } else {
            vibePlayer.stop();
        }
    } catch (err) {
        console.error("[RotectorScan] vibe follow-scans failed", err);
    }
}

/**
 * Ask the host client for connect-src permission for the music host.
 *
 * This is the whole reason the audio is fetched rather than pointed at: on
 * Vencord `raw.githubusercontent.com` is not in the allowlist, and
 * `requestAddOverride` accepts `connect-src` — but not `media-src`, so an
 * `<audio src>` at the same URL could not have been fixed this way at all. The
 * wording matches api/host.ts because it is the same dialog and the same
 * restart. Returns a sentence, shown verbatim.
 */
export async function requestMusicHostPermission(): Promise<string> {
    let csp: any = null;
    try {
        csp = (globalThis as any)?.VencordNative?.csp ?? null;
    } catch {
        csp = null;
    }

    if (!csp?.requestAddOverride) {
        return (
            "This client has no host-permission API — that dialog only exists on the " +
            "desktop client. Nothing was changed."
        );
    }

    let result: string;
    try {
        result = await csp.requestAddOverride(RAW_HOST, ["connect-src"], "RotectorScan");
    } catch (err: any) {
        return `The permission request failed: ${describeError(err)}`;
    }

    switch (result) {
        case "ok":
            return (
                `Allowed. ${RAW_HOST} is now in this client's connect-src list. Fully close ` +
                "and restart Discord — a reload is not enough — and press play again."
            );
        case "cancelled":
            return "You cancelled the permission dialog. Nothing was changed.";
        case "unchecked":
            return "The dialog needs the trust checkbox ticked as well as Allow. Nothing was changed.";
        case "conflict":
            return (
                `${RAW_HOST} already has a rule in this client, so the block is something ` +
                "else — being offline, or a network that does not route to GitHub."
            );
        case "invalid":
            return `The client rejected the request for ${RAW_HOST} as invalid.`;
        default:
            return `The client answered "${String(result)}"; nothing is known to have changed.`;
    }
}

/** Whether the host-permission dialog exists on this build at all. */
export function musicHostPermissionSupported(): boolean {
    try {
        return (globalThis as any)?.VencordNative?.csp?.requestAddOverride != null;
    } catch {
        return false;
    }
}

// --------------------------------------------------------------------------
// helpers
// --------------------------------------------------------------------------

/** `m:ss`, or `--:--` when there is no honest answer. */
export function formatClock(seconds: number): string {
    if (!Number.isFinite(seconds) || seconds < 0) return "--:--";
    const total = Math.floor(seconds);
    const minutes = Math.floor(total / 60);
    const rest = total % 60;
    return `${minutes}:${rest < 10 ? "0" : ""}${rest}`;
}

function describeError(err: any): string {
    if (!err) return "an unknown error.";
    const message = err?.message ? String(err.message) : String(err);
    return message.endsWith(".") ? message : `${message}.`;
}

function newController(): AbortController | null {
    try {
        return typeof AbortController === "undefined" ? null : new AbortController();
    } catch {
        return null;
    }
}

/**
 * `fetch` with a deadline.
 *
 * Everything here talks to somebody else's CDN, and a request that never
 * settles would leave the player on "loading…" for the rest of the session with
 * no way back. The caller's own signal is chained into the deadline's rather
 * than replacing it: passing one through as `signal` would have silently
 * dropped the timeout on exactly the biggest request the plugin makes.
 */
async function timedFetch(url: string, timeoutMs: number, init?: RequestInit): Promise<Response> {
    const caller = init?.signal ?? null;
    const controller = newController();

    let timer: any = null;
    let unchain: (() => void) | null = null;

    if (controller) {
        timer = setTimeout(() => {
            try {
                controller.abort();
            } catch {
                // already aborted
            }
        }, timeoutMs);

        if (caller) {
            if (caller.aborted) {
                try {
                    controller.abort();
                } catch {
                    // as above
                }
            } else {
                const onAbort = () => {
                    try {
                        controller.abort();
                    } catch {
                        // as above
                    }
                };
                caller.addEventListener("abort", onAbort);
                unchain = () => caller.removeEventListener("abort", onAbort);
            }
        }
    }

    try {
        return await fetch(url, {
            method: "GET",
            credentials: "omit",
            referrerPolicy: "no-referrer",
            ...init,
            signal: controller?.signal ?? caller ?? undefined,
        });
    } finally {
        if (timer !== null) clearTimeout(timer);
        unchain?.();
    }
}
