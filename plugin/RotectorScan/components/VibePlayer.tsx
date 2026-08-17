/*
 * RotectorScan — a Discord-client port of rotector-selfbot
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * The player, and the field behind it.
 *
 * This is rsb/tui/vibescreen.py's transport rendered as a Discord surface: the
 * track, where you are in it, and the project page's own device — a fixed
 * barcode whose columns never move and whose brightness is the audio. Which
 * columns are bars never changes; only how lit they are does. That is the
 * difference between the page's field and a chart drawn in bars, and it is why
 * the period and the bar positions are constants in features/vibe.ts rather
 * than something derived from the spectrum.
 *
 * The field is drawn geometry and nothing else: `fillRect` in `currentColor`,
 * no glyphs, no icon font, no images — the same rule the verdict marks follow,
 * and for the same reason (DESIGN.md's marks are drawn, never typed).
 *
 * It is gated twice. `motionAllowed()` reads the plugin's `motion` setting and
 * the system's `prefers-reduced-motion`, and when either says no the canvas is
 * painted once as a still barcode instead of being animated more quietly. A
 * still field is the honest degraded form: the ornament survives, the movement
 * does not.
 *
 * Nothing here autoplays on its own. The player is off by default, it only
 * appears when the user switched vibe mode on, and the first sound follows a
 * click — browsers enforce that anyway, and unrequested audio in somebody's
 * chat client is worse than unrequested audio on a page.
 */

import { Logger } from "@utils/Logger";
import { useEffect, useRef, useState } from "@webpack/common";

import {
    FIELD_BARS,
    FIELD_PERIOD,
    FIELD_SPAN,
    formatClock,
    motionAllowed,
    musicHostPermissionSupported,
    requestMusicHostPermission,
    trackLabel,
    vibeEnabled,
    vibePlayer,
    vibeRepo,
} from "../features/vibe";
import type { VibeState } from "../features/vibe";
import { Btn, cl, Label, Micro } from "./primitives";

const logger = new Logger("RotectorScan");

/**
 * How often the field remembers a level, and therefore how many samples one
 * span of history is.
 *
 * The analyser only knows *now*, so a scrolling field needs its own memory of
 * what it was told. 30 a second is finer than the 20 fps the shipped envelopes
 * were measured at, so the two sources produce the same picture at the same
 * speed rather than one of them running visibly coarser.
 */
const HISTORY_HZ = 30;
const HISTORY_SAMPLES = Math.round(FIELD_SPAN * HISTORY_HZ);

/** the resting brightness of a bar, and how much of the ramp the audio owns */
const BAR_FLOOR = 0.06;
const BAR_RANGE = 0.34;

/** the field's height, in CSS pixels, full and compact */
const FIELD_HEIGHT = 44;
const FIELD_HEIGHT_COMPACT = 18;

// --------------------------------------------------------------------------
// state
// --------------------------------------------------------------------------

/**
 * Subscribe to the player.
 *
 * `useState` + `useEffect` rather than `useSyncExternalStore`, exactly as
 * store.ts does it and for the same reason: React here is whichever version
 * Discord shipped, and a hook that is missing is not a degraded render, it is a
 * thrown error inside somebody's modal.
 */
export function useVibe(): VibeState {
    const [state, setState] = useState<VibeState>(() => vibePlayer.state);

    useEffect(() => {
        // The settings screen is somewhere else entirely, so volume and shuffle
        // are read into the state when a surface mounts rather than watched.
        vibePlayer.sync();
        setState(vibePlayer.state);
        return vibePlayer.subscribe(() => setState(vibePlayer.state));
    }, []);

    return state;
}

// --------------------------------------------------------------------------
// the field
// --------------------------------------------------------------------------

/** `rgb(r, g, b)` -> "r,g,b", so alpha can be varied per bar. */
function inkOf(element: Element | null): string {
    try {
        if (!element) return "255,255,255";
        const colour = getComputedStyle(element).color || "";
        const match = colour.match(/(-?[\d.]+)[,\s]+(-?[\d.]+)[,\s]+(-?[\d.]+)/);
        if (!match) return "255,255,255";
        return `${Math.round(Number(match[1]))},${Math.round(Number(match[2]))},${Math.round(Number(match[3]))}`;
    } catch {
        return "255,255,255";
    }
}

/**
 * The barcode.
 *
 * `aria-hidden`, because it says nothing the transport above it does not: the
 * track, the position and the state are all text. A canvas that carried meaning
 * would need a text equivalent; this one is ornament, and ornament that admits
 * it is ornament is the only kind allowed below full-opacity ink.
 */
function VibeField({ compact, playing }: { compact?: boolean; playing: boolean; }) {
    const canvasRef = useRef<HTMLCanvasElement | null>(null);
    const height = compact ? FIELD_HEIGHT_COMPACT : FIELD_HEIGHT;

    // Whether the field may animate is decided once per mount of this effect,
    // not once per frame: it is two reads (a setting and a media query) and a
    // media query listener is the wrong tool for a value the user changes in a
    // settings screen that is not open.
    const animated = motionAllowed();

    useEffect(() => {
        const canvas = canvasRef.current;
        if (!canvas) return;

        const history = new Float32Array(HISTORY_SAMPLES);
        let head = 0;
        let lastPush = 0;
        let ink = inkOf(canvas);
        let inkAt = 0;
        let frame: number | null = null;
        let stopped = false;

        /** The level `ago` seconds back: the envelope if there is one, our own memory if not. */
        function levelAgo(ago: number): number {
            const fromEnvelope = vibePlayer.levelAgo(ago);
            if (fromEnvelope >= 0) return fromEnvelope;
            if (!playing) return 0;
            let index = head - Math.round(ago * HISTORY_HZ);
            while (index < 0) index += HISTORY_SAMPLES;
            return history[index % HISTORY_SAMPLES] || 0;
        }

        function paint(now: number) {
            const dpr = Math.min(window.devicePixelRatio || 1, 2);
            const width = canvas.clientWidth;
            const box = canvas.clientHeight || height;
            if (!width || !box) return;

            const wantW = Math.round(width * dpr);
            const wantH = Math.round(box * dpr);
            if (canvas.width !== wantW || canvas.height !== wantH) {
                canvas.width = wantW;
                canvas.height = wantH;
            }

            const ctx = canvas.getContext("2d");
            if (!ctx) return;
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            ctx.clearRect(0, 0, width, box);

            // The ink is whatever `currentColor` resolves to here, so the field
            // inverts with `.rsb--invert` and follows a themed client without
            // knowing anything about either. Re-read a second at a time: a theme
            // switch should be picked up, a per-frame getComputedStyle should not.
            if (now - inkAt > 1000) {
                ink = inkOf(canvas);
                inkAt = now;
            }

            for (let x = 0; x < width; x += FIELD_PERIOD) {
                for (const bar of FIELD_BARS) {
                    const px = x + bar[0];
                    if (px >= width) continue;
                    // the left edge is FIELD_SPAN seconds ago, the right edge is now
                    const ago = (1 - px / width) * FIELD_SPAN;
                    const level = animated ? levelAgo(ago) : 0;
                    const alpha = BAR_FLOOR + Math.max(0, Math.min(1, level)) * BAR_RANGE;
                    ctx.fillStyle = `rgba(${ink},${alpha.toFixed(3)})`;
                    ctx.fillRect(px, 0, bar[1], box);
                }
            }
        }

        function tick(now: number) {
            if (stopped) return;
            if (now - lastPush >= 1000 / HISTORY_HZ) {
                lastPush = now;
                head = (head + 1) % HISTORY_SAMPLES;
                history[head] = vibePlayer.sample().level;
            }
            paint(now);
            frame = requestAnimationFrame(tick);
        }

        function still() {
            try {
                paint(0);
            } catch (err) {
                logger.error("vibe field failed to paint", err);
            }
        }

        if (animated && playing) {
            try {
                frame = requestAnimationFrame(tick);
            } catch (err) {
                logger.error("vibe field could not start", err);
                still();
            }
        } else {
            still();
        }

        // A still field still has to survive the modal being resized, and the
        // animated one repaints anyway — one listener covers both.
        const onResize = () => {
            if (!animated || !playing) still();
        };
        window.addEventListener("resize", onResize);

        return () => {
            stopped = true;
            window.removeEventListener("resize", onResize);
            if (frame !== null) {
                try {
                    cancelAnimationFrame(frame);
                } catch {
                    // the frame is not coming back either way
                }
            }
        };
    }, [animated, playing, height]);

    return (
        <canvas
            ref={canvasRef}
            className={cl("vibe__field")}
            aria-hidden="true"
            // Inline, because the canvas has to have a box before style.css is
            // guaranteed to be applied — an unstyled canvas is 300x150 and would
            // knock the whole transport out of shape.
            style={{ display: "block", width: "100%", height: `${height}px` }}
        />
    );
}

// --------------------------------------------------------------------------
// the transport
// --------------------------------------------------------------------------

/** The scrub bar: click anywhere on it to go there, arrows to nudge. */
function SeekBar({ position, duration }: { position: number; duration: number; }) {
    const known = Number.isFinite(duration) && duration > 0;
    const fraction = known ? Math.max(0, Math.min(1, position / duration)) : 0;

    function seekTo(event: any) {
        if (!known) return;
        try {
            const box = event.currentTarget.getBoundingClientRect();
            if (!box.width) return;
            const at = (event.clientX - box.left) / box.width;
            vibePlayer.seek(Math.max(0, Math.min(1, at)) * duration);
        } catch (err) {
            logger.error("seek failed", err);
        }
    }

    function onKeyDown(event: any) {
        if (event.key === "ArrowLeft") {
            event.preventDefault();
            vibePlayer.nudge(-10);
        } else if (event.key === "ArrowRight") {
            event.preventDefault();
            vibePlayer.nudge(10);
        } else if (event.key === "Home") {
            event.preventDefault();
            vibePlayer.seek(0);
        }
    }

    return (
        <div
            className={cl("vibe__seek")}
            role="slider"
            tabIndex={0}
            aria-label="Seek"
            aria-valuemin={0}
            aria-valuemax={known ? Math.round(duration) : 0}
            aria-valuenow={Math.round(position)}
            aria-valuetext={`${formatClock(position)} of ${known ? formatClock(duration) : "an unknown length"}`}
            onClick={seekTo}
            onKeyDown={onKeyDown}
        >
            <span className={cl("vibe__fill")} style={{ width: `${(fraction * 100).toFixed(2)}%` }} />
        </div>
    );
}

/** What the player is doing, as one word, for the header. */
function stateWord(state: VibeState): string {
    switch (state.status) {
        case "playing": return "playing";
        case "paused": return state.blocked ? "waiting for a click" : "paused";
        case "loading": return state.fetching ? "fetching the track" : "reading the library";
        case "error": return "stopped";
        case "ready": return state.library ? `${state.library} track(s)` : "ready";
        default: return "off";
    }
}

/**
 * Open a URL the way the host client wants it opened.
 *
 * A bare `<a href>` inside the renderer can navigate Discord itself, which
 * would drop whatever the moderator was doing, so the native opener is tried
 * first and `window.open` is the web fallback. (The same helper ReportModal
 * uses; it is small enough that a shared import is not worth a new module.)
 */
function openExternal(url: string): void {
    if (!url) return;
    // The link comes out of somebody else's manifest, so the scheme is checked
    // here rather than trusted: `openExternal` hands whatever it is to the
    // operating system, and a `javascript:` or `file:` entry in a JSON file
    // nobody in this project controls must not become a click target.
    try {
        const scheme = new URL(url).protocol;
        if (scheme !== "https:" && scheme !== "http:") {
            logger.warn(`refusing to open a ${scheme} link from the music manifest`);
            return;
        }
    } catch {
        return;   // not a URL at all
    }
    try {
        const native = (window as any)?.VencordNative?.native;
        if (native && typeof native.openExternal === "function") {
            native.openExternal(url);
            return;
        }
    } catch {
        // fall through to window.open
    }
    try {
        window.open(url, "_blank", "noreferrer,noopener");
    } catch (err) {
        logger.error(`could not open ${url}`, err);
    }
}

/**
 * The player.
 *
 * Returns null when vibe mode is switched off, which is the default: this is a
 * music player inside somebody's chat client, and it exists because a scan of
 * ten thousand members against an aggregating backend takes half an hour.
 * `compact` is the form for a modal footer — the field, the title and three
 * buttons, with the seek bar and the credits left to the full one.
 */
export function VibeControls({ compact }: { compact?: boolean; }): JSX.Element | null {
    const state = useVibe();
    const [permission, setPermission] = useState("");
    const [asking, setAsking] = useState(false);
    const alive = useRef(true);

    const enabled = vibeEnabled();

    useEffect(() => {
        alive.current = true;
        return () => {
            alive.current = false;
        };
    }, []);

    // The manifest is a few hundred bytes and nothing else can be shown without
    // it — not the track count, not a title. It is fetched when a player is on
    // screen and never at plugin start, so a user who never opens one never
    // touches the music host at all.
    useEffect(() => {
        if (!enabled) return;
        if (state.status !== "idle") return;
        try {
            void vibePlayer.load();
        } catch (err) {
            logger.error("vibe library failed to load", err);
        }
    }, [enabled, state.status]);

    if (!enabled) return null;

    const playing = state.status === "playing";
    const track = state.track;
    const busy = state.status === "loading";

    function grant() {
        setAsking(true);
        Promise.resolve()
            .then(() => requestMusicHostPermission())
            .then(outcome => {
                if (alive.current) setPermission(String(outcome));
            })
            .catch(err => {
                if (alive.current) setPermission(String(err?.message ?? err));
            })
            .then(() => {
                if (alive.current) setAsking(false);
            });
    }

    const failed = state.status === "error";

    return (
        <div className={`rsb ${cl("vibe", compact && "vibe--compact")}`}>
            <div className={cl("vibe__hd")}>
                <Label>VIBE</Label>
                <Micro>{stateWord(state)}</Micro>
            </div>

            <VibeField compact={compact} playing={playing} />

            <div className={cl("vibe__now")}>
                {track ? trackLabel(track) : busy ? "Reading the library…" : "Nothing playing."}
            </div>

            {!compact && track && (track.license || track.source) ? (
                <div className={cl("vibe__meta")}>
                    {track.license ? <Micro>{track.license}</Micro> : null}
                    {track.source ? (
                        <button
                            type="button"
                            className={`${cl("link")} ${cl("vibe__source")}`}
                            onClick={() => openExternal(track.source)}
                        >
                            source
                        </button>
                    ) : null}
                </div>
            ) : null}

            {!compact ? (
                <>
                    <SeekBar position={state.position} duration={state.duration} />
                    <div className={cl("vibe__time")}>
                        <Micro>{formatClock(state.position)}</Micro>
                        <Micro>{state.duration > 0 ? formatClock(state.duration) : "--:--"}</Micro>
                    </div>
                </>
            ) : null}

            <div className={cl("actions")}>
                {!compact ? (
                    <Btn quiet onClick={() => vibePlayer.prev()}>Prev</Btn>
                ) : null}
                <Btn onClick={() => vibePlayer.toggle()}>{playing ? "Pause" : "Play"}</Btn>
                <Btn quiet onClick={() => vibePlayer.next()}>Next</Btn>
                {!compact ? (
                    <Btn quiet onClick={() => vibePlayer.stop()}>Stop</Btn>
                ) : null}
            </div>

            {state.error ? <Micro>{state.error}</Micro> : null}

            {failed && musicHostPermissionSupported() ? (
                <div className={cl("actions")}>
                    <Btn quiet disabled={asking} onClick={grant}>
                        {asking ? "Asking" : "Grant music host permission"}
                    </Btn>
                </div>
            ) : null}
            {permission ? <Micro>{permission}</Micro> : null}

            {!compact ? (
                <Micro>
                    Streamed one track at a time from {vibeRepo()} — the library is that
                    repository's index.json, nothing is downloaded to disk, and each track is
                    released from memory when it ends. Licences are shown because royalty-free
                    is not the same as free of obligations.
                </Micro>
            ) : null}
        </div>
    );
}

/**
 * The field on its own, for a surface that already has its own transport.
 *
 * Exported because the scan modal may want the barcode across the top of a
 * running scan without a second set of buttons in it.
 */
export function VibeBarcode({ compact }: { compact?: boolean; }): JSX.Element | null {
    const state = useVibe();
    if (!vibeEnabled()) return null;
    return <VibeField compact={compact} playing={state.status === "playing"} />;
}
