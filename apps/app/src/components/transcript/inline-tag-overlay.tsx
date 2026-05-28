"use client";

import type { InlineTag } from "@/lib/interactions";

/**
 * Inline tag overlay for transcript turns.
 *
 * Each LLM-emitted ``inline_tag`` carries a start/end timestamp + a
 * type. We pin a tag to a transcript turn by matching the turn's time
 * against the tag's range. The turn renders its text wrapped in a
 * colored highlight + a hover popup with the tag's popup_text and
 * suggested action.
 *
 * Visual language follows the Phase 5 palette spec — color + matching
 * text color, distinguished by hue only. Color-blind users get the
 * same content via the popup (which is the source of truth).
 */
const PALETTE: Record<
    string,
    { bg: string; fg: string; label: string }
> = {
    went_well: { bg: "#E2F4E0", fg: "#1F6B30", label: "Went well" },
    improvement: { bg: "#FCEFD9", fg: "#8B5A0F", label: "Could improve" },
    competitor: { bg: "#DDEBF8", fg: "#1B4F8C", label: "Competitor" },
    commitment: { bg: "#F4EBC2", fg: "#7A5A0A", label: "Commitment" },
    objection_resolved: { bg: "#D4EDE7", fg: "#1A6457", label: "Objection. resolved" },
    objection_unresolved: { bg: "#FADCD8", fg: "#9A2A1F", label: "Objection. unresolved" },
    tense: { bg: "#FFF6CC", fg: "#7A6308", label: "Tense moment" },
};

export function paletteFor(type: string): { bg: string; fg: string; label: string } {
    return (
        PALETTE[type] ?? {
            bg: "#E8E8E8",
            fg: "#5A5A5A",
            label: type.replace(/_/g, " "),
        }
    );
}

/**
 * Find the tag (if any) whose time range covers ``turnTime``.
 * Tags are time-ranges; turnTime is a single point. Cheap linear
 * scan — transcripts rarely have more than 50-100 tags total, and
 * even a 60-min call has <500 turns to scan against.
 */
export function findTagForTurn(
    tags: InlineTag[] | undefined,
    turnTime: string,
): InlineTag | undefined {
    if (!tags || tags.length === 0) return undefined;
    const t = parseTimeToSeconds(turnTime);
    if (Number.isNaN(t)) return undefined;
    return tags.find((tag) => {
        const a = parseTimeToSeconds(tag.start_time);
        const b = parseTimeToSeconds(tag.end_time);
        if (Number.isNaN(a) || Number.isNaN(b)) return false;
        return t >= a && t <= b;
    });
}

/**
 * Wrap a transcript turn's text with the inline-tag highlight.
 * Renders the popup on hover (CSS `group-hover`) so the rep can
 * read the context without leaving the page.
 */
export function TaggedTurnText({
    text,
    tag,
}: {
    text: string;
    tag: InlineTag | undefined;
}) {
    if (!tag) return <span>{text}</span>;
    const palette = paletteFor(tag.type);
    return (
        <span
            className="group relative cursor-help rounded px-1"
            style={{ backgroundColor: palette.bg, color: palette.fg }}
        >
            {text}
            <span
                role="tooltip"
                className="pointer-events-none absolute left-0 top-full z-30 mt-1 hidden w-72 rounded-md border border-border bg-bg-card p-2 text-xs text-text shadow-lg group-hover:block"
            >
                <div
                    className="mb-1 font-semibold"
                    style={{ color: palette.fg }}
                >
                    {palette.label}
                </div>
                <div>{tag.popup_text}</div>
                {tag.suggested_action && (
                    <div className="mt-1 text-text-muted">
                        <span className="font-medium">Try:</span>{" "}
                        {tag.suggested_action}
                    </div>
                )}
            </span>
        </span>
    );
}

function parseTimeToSeconds(raw: string): number {
    const s = String(raw ?? "").trim();
    if (s.includes(":")) {
        const parts = s.split(":").map((p) => parseInt(p, 10));
        if (parts.some((p) => Number.isNaN(p))) return NaN;
        let secs = 0;
        for (const p of parts) secs = secs * 60 + p;
        return secs;
    }
    const n = parseFloat(s);
    return Number.isNaN(n) ? NaN : n;
}
