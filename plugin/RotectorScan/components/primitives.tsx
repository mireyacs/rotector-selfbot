/*
 * RotectorScan — a Discord-client port of rotector-selfbot
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * The shared render pieces. Everything the modals and the decorator draw is
 * built out of these, so that the design rules live in one file instead of
 * being re-decided at every call site.
 *
 * Two of those rules are enforced here rather than merely followed:
 *
 *  - Every element carries the `rsb` base class as well as its own `rsb-*`
 *    class. The token block in style.css is defined on `.rsb` rather than on
 *    `:root`, so a node that forgets the base class silently loses the whole
 *    palette and inherits Discord's instead.
 *  - `VerdictLine` will not print a verdict word without its meaning for
 *    NO_DETECTIONS. Rotector's Terms rule 3 forbids presenting an unflagged
 *    user as "Safe", and the meaning string is the only thing on screen that
 *    says so.
 *
 * There are no glyphs, icon components or emoji anywhere in this file. Marks
 * are drawn geometry — a masked stripe field taking its colour from the verdict
 * — because a drawn mark inverts and recolours for free and a typed codepoint
 * does neither (DESIGN.md:611-616).
 */

import { classNameFactory } from "@utils/css";

import { Verdict, verdictLabel, verdictMeaning, verdictName } from "../api/verdict";

import type { ReactNode } from "react";

export const cl = classNameFactory("rsb-");

/**
 * The class attribute for one of our elements: the token-carrying base class
 * plus the element's own class. Written out rather than folded into `cl`
 * because `cl` is the shared Equicord helper and other modules call it too.
 */
function rsb(...names: string[]): string {
    return `rsb ${cl(...names)}`;
}

/**
 * A verdict that is definitely one of the five.
 *
 * The plugin is loaded without type checking, so an out-of-range number can
 * reach these components from a hand-edited setting or a future API value. An
 * unrecognised verdict must not produce `data-verdict="undefined"`: that would
 * leave the mask unset and paint a solid block of colour, which is the loudest
 * possible mark for the least certain finding.
 */
function safeVerdict(verdict: unknown): Verdict {
    const v = Number(verdict);
    if (v === Verdict.NO_DETECTIONS || v === Verdict.INFO || v === Verdict.CAUTION || v === Verdict.THREAT) {
        return v as Verdict;
    }
    return Verdict.UNKNOWN;
}

/** The `data-verdict` attribute value, e.g. "THREAT" — the convention rsb/htmlrender.py emits. */
function verdictAttr(verdict: unknown): string {
    return verdictName(safeVerdict(verdict));
}

// --------------------------------------------------------------------------
// ornament
// --------------------------------------------------------------------------

/**
 * The masked stripe field that fills a node header.
 *
 * This is the No Grey Fill Rule made literal: a header separates from its body
 * by density, not by a lighter panel. It is pure ornament, so it is hidden from
 * assistive technology and cannot be selected — the Pure Ink Rule's price for
 * being allowed to sit below full-opacity ink.
 */
export function Chip({ className }: { className?: string; }) {
    return (
        <span
            className={className ? `${rsb("chip")} ${className}` : rsb("chip")}
            aria-hidden="true"
        />
    );
}

/**
 * The drawn mark: a stripe field whose duty cycle encodes the tier.
 *
 * Colour is never the only carrier. The stripe period differs per verdict as
 * well as the hue, and either `title` names the finding or the mark is ornament
 * beside text that already does.
 */
export function Meter({
    verdict,
    wide,
    pending,
    title,
}: {
    verdict: Verdict;
    wide?: boolean;
    pending?: boolean;
    title?: string;
}) {
    const v = safeVerdict(verdict);
    const labelled = typeof title === "string" && title.length > 0;

    return (
        <span
            className={rsb("meter", wide && "meter--wide", pending && "meter--pending")}
            data-verdict={verdictAttr(v)}
            role={labelled ? "img" : undefined}
            aria-label={labelled ? title : undefined}
            aria-hidden={labelled ? undefined : "true"}
        />
    );
}

// --------------------------------------------------------------------------
// type
// --------------------------------------------------------------------------

/** The label step: uppercase, wide-tracked, used for column and node headings. */
export function Label({ children }: { children: ReactNode; }) {
    return <span className={rsb("label")}>{children}</span>;
}

/**
 * The floor of the ramp. Ids, rule handles, retention notices.
 *
 * Micro type is small, but it is still content, so it stays full-opacity ink.
 * Anything that wants to be dimmed is ornament and belongs in `Chip`.
 */
export function Micro({ children }: { children: ReactNode; }) {
    return <span className={rsb("micro")}>{children}</span>;
}

/** Reading copy. `measure` caps the line length at 68ch for prose. */
export function Body({ children, measure }: { children: ReactNode; measure?: boolean; }) {
    return <div className={rsb("body", measure && "body--measure")}>{children}</div>;
}

/**
 * The verdict word in the verdict hue, and its meaning in pure ink.
 *
 * `meaning` may be switched off where the meaning is carried adjacently (a
 * tooltip, a ledger row whose header explains the column) — except for
 * NO_DETECTIONS, which always prints it. "Linked account(s) known but unflagged.
 * Not a clean bill of health." is the sentence that stops a green word from
 * reading as a clearance, and Rotector's terms require it.
 */
export function VerdictLine({ verdict, meaning }: { verdict: Verdict; meaning?: boolean; }) {
    const v = safeVerdict(verdict);
    const showMeaning = meaning !== false || v === Verdict.NO_DETECTIONS;

    return (
        <div className={rsb("verdict-line")} data-verdict={verdictAttr(v)}>
            <span className={`${cl("verdict")} ${cl("action")}`}>{verdictLabel(v)}</span>
            {showMeaning && <span className={`${cl("meaning")} ${cl("body")}`}>{verdictMeaning(v)}</span>}
        </div>
    );
}

// --------------------------------------------------------------------------
// containers
// --------------------------------------------------------------------------

/**
 * A hairline-framed rectangle with a LABEL header.
 *
 * The header's remaining width is filled by the stripe chip, which is what
 * gives the frame its weight without a fill. Counts live in the header, beside
 * the label, because a section that says how many things it holds is a section
 * a reader can trust when it is empty.
 */
export function Node({
    label,
    count,
    chip,
    children,
}: {
    label: string;
    count?: number;
    chip?: boolean;
    children: ReactNode;
}) {
    const showChip = chip !== false;
    const hasCount = typeof count === "number" && isFinite(count);

    return (
        <div className={rsb("node")}>
            <div className={cl("node__hd")}>
                <span className={cl("node__name")}>{label}</span>
                {hasCount && <span className={cl("node__count")}>{`(${count})`}</span>}
                {showChip && <Chip />}
            </div>
            <div className={cl("node__bd")}>{children}</div>
        </div>
    );
}

/** One `KEY  value` line. The key is micro type; the value is whatever it is. */
export function KeyVal({ k, v }: { k: string; v: ReactNode; }) {
    return (
        <div className={rsb("kv")}>
            <span className={cl("kv__k")}>{k}</span>
            <span className={cl("kv__v")}>{v}</span>
        </div>
    );
}

/** An empty state is a sentence saying what is not here, never an illustration. */
export function Empty({ children }: { children: ReactNode; }) {
    return <div className={rsb("empty")}>{children}</div>;
}

// --------------------------------------------------------------------------
// action
// --------------------------------------------------------------------------

/**
 * A square button: ink face, ground text, inverting on hover.
 *
 * `quiet` is the secondary form — transparent with a quiet-stroke outline.
 * Disabled draws as a hairline outline rather than a greyed face, because grey
 * is a line in this system and never a surface.
 */
export function Btn({
    quiet,
    disabled,
    onClick,
    children,
}: {
    quiet?: boolean;
    disabled?: boolean;
    onClick(): void;
    children: ReactNode;
}) {
    return (
        <button
            type="button"
            className={rsb("btn", quiet && "btn--quiet")}
            disabled={Boolean(disabled)}
            aria-disabled={disabled ? "true" : undefined}
            onClick={() => {
                if (disabled) return;
                // a click handler that throws inside a Discord modal takes the
                // modal down with it, so failures stay local and visible in the
                // console rather than blanking the surface
                try {
                    onClick?.();
                } catch (err) {
                    console.error("[RotectorScan] button handler failed", err);
                }
            }}
        >
            {children}
            {/*
              * The tick: the page's third piece of motion vocabulary
              * (`html.motion .btn .tick`), which the first port left out.
              *
              * It is a masked stripe strip in the button face, aria-hidden and
              * unselectable like every other ornament here, and it takes
              * `currentColor` — so it inverts with the face on hover for free
              * rather than needing a second rule to stay visible.
              */}
            <span className={cl("tick")} aria-hidden="true" />
        </button>
    );
}
