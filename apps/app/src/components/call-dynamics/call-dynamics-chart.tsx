"use client";

import { useMemo, useState } from "react";
import type { SentimentTrajectoryPoint } from "@/lib/interactions";
import { useContextDrawer } from "@/components/context-drawer/context-drawer";

interface KeyMoment {
    time: string;
    type: string;
    description: string;
    start_time?: string;
    end_time?: string;
}

const PIN_TYPES = [
    "objection",
    "commitment",
    "question",
    "risk",
    "win",
    "other",
] as const;

/** Categorize a free-form ``type`` into one of the visible pin filters. */
function pinCategory(type: string): (typeof PIN_TYPES)[number] {
    const t = type.toLowerCase();
    if (t.includes("objection")) return "objection";
    if (t.includes("commit")) return "commitment";
    if (t.includes("question")) return "question";
    if (t.includes("risk") || t.includes("churn") || t.includes("warning"))
        return "risk";
    if (t.includes("win") || t.includes("positive") || t.includes("success"))
        return "win";
    return "other";
}

const PIN_COLORS: Record<(typeof PIN_TYPES)[number], string> = {
    objection: "var(--accent-rose)",
    commitment: "var(--accent-emerald)",
    question: "var(--accent-cyan)",
    risk: "var(--accent-amber)",
    win: "var(--primary)",
    other: "var(--text-subtle)",
};

// One-line definition per pin category so the legend doesn't leave the
// rep guessing what each color means.
const PIN_DEFINITIONS: Record<(typeof PIN_TYPES)[number], string> = {
    win: "Deal advanced or closed; clear forward momentum.",
    commitment: "Customer or rep committed to a specific next action.",
    objection: "Customer pushed back on price, timing, fit, or risk.",
    risk: "Churn or compliance warning; needs follow-up.",
    question: "Open question raised on the call that still needs an answer.",
    other: "Notable moment that doesn't fit the categories above.",
};

// Severity weights drive cluster ranking: a single objection is worth
// more visual airtime than three "other" mentions because reps need to
// know about it. Tuned so a cluster with 1 objection beats a cluster
// with 2 questions, but a "win" still beats an "other".
const PIN_SEVERITY: Record<(typeof PIN_TYPES)[number], number> = {
    objection: 3,
    risk: 3,
    commitment: 2,
    win: 2,
    question: 1,
    other: 0.5,
};

function clusterSeverity(items: Array<{ type: string }>): number {
    let total = 0;
    for (const m of items) {
        total += PIN_SEVERITY[pinCategory(m.type)];
    }
    return total;
}

/**
 * Call Dynamics — single timeline with up to three toggleable layers:
 * customer mood (sentiment_trajectory), customer vocal energy (paralinguistics
 * — when available), rep talk-density (call metrics — when available).
 *
 * Click anywhere on the chart fires ``onSeek(seconds)`` so the parent
 * can scroll the transcript or play audio from that moment.
 *
 * v1 ships the mood layer (always available from analysis output) and
 * leaves slots for energy + talk-density to plug in once their data
 * sources are wired through to insights. The rest of the visual
 * scaffolding is in place so adding them is a same-file edit.
 */
export function CallDynamicsChart({
    trajectory,
    keyMoments,
    durationSeconds,
    onSeek,
}: {
    trajectory: SentimentTrajectoryPoint[] | undefined;
    keyMoments?: KeyMoment[];
    durationSeconds?: number | null;
    onSeek?: (seconds: number) => void;
}) {
    const points = useMemo(
        () => normalizeTrajectory(trajectory, durationSeconds),
        [trajectory, durationSeconds],
    );
    const [pinFilter, setPinFilter] = useState<
        Set<(typeof PIN_TYPES)[number]>
    >(() => new Set(PIN_TYPES));
    const drawer = useContextDrawer();

    if (points.length === 0) {
        return null;
    }

    const width = 720;
    const height = 140;
    const padX = 60; // wide enough to fit the y-axis band labels (Positive / Neutral / Frustrated)
    const padY = 16;
    const innerW = width - padX * 2;
    const innerH = height - padY * 2;

    const maxT = points[points.length - 1].t;
    const minT = points[0].t;
    const span = Math.max(1, maxT - minT);

    const xFor = (t: number) => padX + ((t - minT) / span) * innerW;
    const yFor = (s: number) => padY + (1 - clamp01(s / 10)) * innerH;

    const path = points
        .map((p, i) => `${i === 0 ? "M" : "L"}${xFor(p.t).toFixed(1)},${yFor(p.score).toFixed(1)}`)
        .join(" ");

    const fillPath =
        path +
        ` L${xFor(maxT).toFixed(1)},${(padY + innerH).toFixed(1)} L${xFor(minT).toFixed(1)},${(padY + innerH).toFixed(1)} Z`;

    function handleClick(e: React.MouseEvent<SVGSVGElement>) {
        if (!onSeek) return;
        const rect = e.currentTarget.getBoundingClientRect();
        const x = e.clientX - rect.left;
        // Map the click x back to seconds using the same transform.
        const t = ((x - padX) / innerW) * span + minT;
        onSeek(Math.max(0, Math.round(t)));
    }

    return (
        <section className="rounded-lg border border-border bg-bg-card p-5">
            <header className="mb-2 flex items-baseline justify-between gap-2">
                <h3 className="text-sm font-semibold">Call dynamics</h3>
                <span className="text-xs text-text-subtle">
                    Customer mood over time · click to jump
                </span>
            </header>
            <svg
                role="img"
                aria-label="Customer mood across the call"
                viewBox={`0 0 ${width} ${height}`}
                className="block w-full cursor-pointer"
                onClick={handleClick}
            >
                {/* y-axis grid: subtle reference lines at 2.5 / 5 / 7.5 (negative / neutral / positive bands). */}
                {[2.5, 5, 7.5].map((s) => (
                    <line
                        key={s}
                        x1={padX}
                        x2={width - padX}
                        y1={yFor(s)}
                        y2={yFor(s)}
                        stroke="var(--border-light)"
                        strokeWidth={1}
                        strokeDasharray="2 4"
                    />
                ))}
                {/* y-axis labels — three bands of customer mood. Renders to
                    the LEFT of the chart so the rep can decode the line at
                    a glance. */}
                {[
                    { score: 8, label: "Positive" },
                    { score: 5, label: "Neutral" },
                    { score: 2, label: "Frustrated" },
                ].map(({ score, label }) => (
                    <text
                        key={label}
                        x={padX - 4}
                        y={yFor(score) + 3}
                        fontSize={9}
                        fill="var(--text-subtle)"
                        textAnchor="end"
                    >
                        {label}
                    </text>
                ))}
                {/* mood fill + line */}
                <path d={fillPath} fill="var(--primary-soft)" fillOpacity={0.5} />
                <path
                    d={path}
                    fill="none"
                    stroke="var(--primary)"
                    strokeWidth={2}
                    strokeLinejoin="round"
                />
                {/* Vertices for keyboard-friendly hit targets and per-point hover. */}
                {points.map((p, i) => (
                    <circle
                        key={i}
                        cx={xFor(p.t)}
                        cy={yFor(p.score)}
                        r={3}
                        fill="var(--primary)"
                    >
                        <title>
                            {formatSeconds(p.t)}. sentiment {p.score.toFixed(1)}/10
                        </title>
                    </circle>
                ))}
                {/* Key-moment pins layered on top of the mood line.
                    Clustered into bins ~20s wide to avoid overlap on
                    busy calls; the busiest 6 bins render visibly,
                    the rest hide behind a "+N more" indicator. */}
                {(() => {
                    const moments = (keyMoments || []).filter((m) =>
                        pinFilter.has(pinCategory(m.type)),
                    );
                    if (moments.length === 0) return null;
                    const clusters = clusterMoments(moments, 20);
                    // Rank by severity (a single objection beats two
                    // "other" mentions). Ties broken by raw count, so
                    // bigger clusters of equal severity still win.
                    const ranked = [...clusters].sort((a, b) => {
                        const sa = clusterSeverity(a.items);
                        const sb = clusterSeverity(b.items);
                        if (sb !== sa) return sb - sa;
                        return b.items.length - a.items.length;
                    });
                    const visible = ranked.slice(0, 6);
                    return visible.map((cluster, idx) => {
                        const t = cluster.tCenter;
                        const cx = xFor(t);
                        const cy = yFor(0); // pin row at the bottom of the chart
                        const dominantType = cluster.items[0].type;
                        const color = PIN_COLORS[pinCategory(dominantType)];
                        return (
                            <g key={idx}>
                                <line
                                    x1={cx}
                                    x2={cx}
                                    y1={padY}
                                    y2={cy}
                                    stroke={color}
                                    strokeOpacity={0.25}
                                    strokeWidth={1}
                                />
                                <circle
                                    cx={cx}
                                    cy={padY + innerH - 4}
                                    r={(() => {
                                        // Severity-weighted radius —
                                        // a 1-item objection (sev=3)
                                        // looks at least as big as a
                                        // 2-item question cluster
                                        // (sev=2).
                                        const sev = clusterSeverity(
                                            cluster.items,
                                        );
                                        return Math.min(8, 3 + sev * 0.7);
                                    })()}
                                    fill={color}
                                    stroke="var(--bg-card)"
                                    strokeWidth={1.5}
                                    style={{ cursor: "pointer" }}
                                    onClick={(e) => {
                                        e.stopPropagation();
                                        // Open a drawer with full details
                                        // of every moment in this cluster.
                                        // The rep can read the descriptions
                                        // and then jump to the transcript
                                        // from inside the drawer.
                                        drawer.open({
                                            title: `Moments at ${formatSeconds(t)}`,
                                            body: (
                                                <ClusterDrawerBody
                                                    items={cluster.items}
                                                    timeSeconds={t}
                                                    onJump={onSeek}
                                                />
                                            ),
                                        });
                                    }}
                                >
                                    <title>
                                        {formatSeconds(t)} -{" "}
                                        {cluster.items
                                            .slice(0, 3)
                                            .map(
                                                (m) =>
                                                    `${m.type}: ${m.description}`,
                                            )
                                            .join("; ")}
                                        {cluster.items.length > 3
                                            ? ` (+${cluster.items.length - 3} more)`
                                            : ""}
                                    </title>
                                </circle>
                                {cluster.items.length > 1 && (
                                    <text
                                        x={cx}
                                        y={padY + innerH - 1}
                                        fontSize={9}
                                        fill="white"
                                        textAnchor="middle"
                                        dominantBaseline="middle"
                                        style={{ pointerEvents: "none" }}
                                    >
                                        {cluster.items.length}
                                    </text>
                                )}
                            </g>
                        );
                    });
                })()}
                {/* x-axis labels: start, middle, end. */}
                <text
                    x={padX}
                    y={height - 2}
                    fontSize={10}
                    fill="var(--text-subtle)"
                >
                    {formatSeconds(minT)}
                </text>
                <text
                    x={width - padX}
                    y={height - 2}
                    fontSize={10}
                    fill="var(--text-subtle)"
                    textAnchor="end"
                >
                    {formatSeconds(maxT)}
                </text>
            </svg>
            {keyMoments && keyMoments.length > 0 && (
                <div className="mt-2 space-y-2 text-xs">
                    <div className="flex flex-wrap items-center gap-1">
                        <span className="text-text-muted">Filter:</span>
                        {PIN_TYPES.map((type) => {
                            const active = pinFilter.has(type);
                            return (
                                <button
                                    key={type}
                                    type="button"
                                    title={PIN_DEFINITIONS[type]}
                                    onClick={() =>
                                        setPinFilter((prev) => {
                                            const next = new Set(prev);
                                            if (next.has(type)) next.delete(type);
                                            else next.add(type);
                                            return next;
                                        })
                                    }
                                    className={`rounded-full border px-2 py-0.5 capitalize transition-colors ${
                                        active
                                            ? "border-transparent text-white"
                                            : "border-border bg-bg-secondary text-text-muted"
                                    }`}
                                    style={
                                        active
                                            ? { backgroundColor: PIN_COLORS[type] }
                                            : undefined
                                    }
                                >
                                    {type}
                                </button>
                            );
                        })}
                    </div>
                    {/* Visible legend so the rep can decode the colors
                        without hovering each filter button. */}
                    <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-[11px] text-text-muted sm:grid-cols-3">
                        {PIN_TYPES.map((type) => (
                            <div key={type} className="flex items-start gap-1.5">
                                <span
                                    aria-hidden
                                    className="mt-1 inline-block h-2 w-2 shrink-0 rounded-full"
                                    style={{ backgroundColor: PIN_COLORS[type] }}
                                />
                                <span>
                                    <span className="font-medium capitalize text-text">
                                        {type}.
                                    </span>{" "}
                                    {PIN_DEFINITIONS[type]}
                                </span>
                            </div>
                        ))}
                    </dl>
                </div>
            )}
            <p className="mt-1 text-xs text-text-subtle">
                Hover any point for the timestamp and score. Click anywhere
                on the chart to jump the transcript to that moment.
            </p>
        </section>
    );
}

interface NormalizedPoint {
    t: number; // seconds since start
    score: number; // 0-10
}

function normalizeTrajectory(
    raw: SentimentTrajectoryPoint[] | undefined,
    durationSeconds?: number | null,
): NormalizedPoint[] {
    if (!raw || raw.length === 0) return [];
    const points = raw
        .map((p) => ({
            t: parseTimeToSeconds(p.time, durationSeconds),
            score: typeof p.score === "number" ? p.score : 0,
        }))
        .filter((p) => !Number.isNaN(p.t))
        .sort((a, b) => a.t - b.t);
    // Dedupe consecutive points at the same timestamp — the LLM
    // sometimes emits 00:30 twice.
    const out: NormalizedPoint[] = [];
    for (const p of points) {
        if (out.length === 0 || out[out.length - 1].t !== p.t) out.push(p);
    }
    return out;
}

function parseTimeToSeconds(raw: string, durationSeconds?: number | null): number {
    const s = String(raw ?? "").trim();
    // ``HH:MM:SS`` or ``MM:SS`` (LLM convention from the prompt).
    if (s.includes(":")) {
        const parts = s.split(":").map((p) => parseInt(p, 10));
        if (parts.some((p) => Number.isNaN(p))) return NaN;
        let secs = 0;
        for (const p of parts) secs = secs * 60 + p;
        return secs;
    }
    // Bare number → already seconds. Some prompts emit fractional
    // ``0.0..1.0`` progress; if the value is sub-1 and we have a
    // duration, scale it.
    const num = parseFloat(s);
    if (Number.isNaN(num)) return NaN;
    if (num >= 0 && num <= 1 && durationSeconds && durationSeconds > 0) {
        return Math.round(num * durationSeconds);
    }
    return num;
}

function formatSeconds(t: number): string {
    const total = Math.max(0, Math.round(t));
    const m = Math.floor(total / 60);
    const s = total % 60;
    return `${m}:${s.toString().padStart(2, "0")}`;
}

function clamp01(v: number): number {
    return Math.max(0, Math.min(1, v));
}

interface MomentCluster {
    tCenter: number;
    items: KeyMoment[];
}

/**
 * Group moments whose timestamps are within ``windowSec`` of each
 * other into a single visual cluster. The cluster's ``tCenter`` is
 * the midpoint of its members' timestamps, which renders the pin
 * close to where the rep would expect.
 */
function clusterMoments(moments: KeyMoment[], windowSec: number): MomentCluster[] {
    const sorted = moments
        .map((m) => ({
            t: parseTimeToSeconds(m.start_time ?? m.time, null),
            m,
        }))
        .filter((x) => !Number.isNaN(x.t))
        .sort((a, b) => a.t - b.t);
    const clusters: MomentCluster[] = [];
    for (const { t, m } of sorted) {
        const last = clusters[clusters.length - 1];
        if (last && t - last.tCenter <= windowSec) {
            last.items.push(m);
            // Update tCenter to the running mean — keeps the pin
            // visually anchored to the cluster's mass.
            const total = last.items.reduce((acc, item, i) => {
                const tv = parseTimeToSeconds(
                    item.start_time ?? item.time,
                    null,
                );
                return acc + (Number.isNaN(tv) ? last.tCenter : tv);
            }, 0);
            last.tCenter = total / last.items.length;
        } else {
            clusters.push({ tCenter: t, items: [m] });
        }
    }
    return clusters;
}

/**
 * Drawer body rendered when the rep clicks a chart pin (or cluster).
 *
 * Lists every moment in the cluster with its description and offers a
 * "Jump to transcript at HH:MM" button so the rep can land on the
 * source segment. Replaces the prior behavior where pin clicks just
 * seek-fired with no detail.
 */
function ClusterDrawerBody({
    items,
    timeSeconds,
    onJump,
}: {
    items: KeyMoment[];
    timeSeconds: number;
    onJump?: (seconds: number) => void;
}) {
    return (
        <div className="space-y-3 text-sm text-text">
            <p className="text-xs text-text-subtle">
                {items.length === 1
                    ? "One moment at "
                    : `${items.length} moments grouped near `}
                <span className="font-medium">{formatSeconds(timeSeconds)}</span>.
            </p>
            <ul className="space-y-2">
                {items.map((m, i) => {
                    const cat = pinCategory(m.type);
                    return (
                        <li
                            key={i}
                            className="rounded border border-border-light bg-bg-secondary p-2"
                        >
                            <div className="flex items-baseline justify-between gap-2">
                                <span
                                    className="inline-flex items-center gap-1.5 text-[11px] uppercase tracking-wide"
                                    style={{ color: PIN_COLORS[cat] }}
                                >
                                    <span
                                        aria-hidden
                                        className="inline-block h-1.5 w-1.5 rounded-full"
                                        style={{ backgroundColor: PIN_COLORS[cat] }}
                                    />
                                    {m.type}
                                </span>
                                <span className="text-[11px] text-text-subtle">
                                    {m.start_time || m.time}
                                </span>
                            </div>
                            <p className="mt-1 text-xs text-text">{m.description}</p>
                        </li>
                    );
                })}
            </ul>
            {onJump && (
                <button
                    type="button"
                    onClick={() => onJump(Math.round(timeSeconds))}
                    className="rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-white hover:bg-primary-hover"
                >
                    Jump to transcript at {formatSeconds(timeSeconds)}
                </button>
            )}
        </div>
    );
}
