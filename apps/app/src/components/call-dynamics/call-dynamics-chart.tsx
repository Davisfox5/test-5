"use client";

import { useMemo } from "react";
import type { SentimentTrajectoryPoint } from "@/lib/interactions";

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
    durationSeconds,
    onSeek,
}: {
    trajectory: SentimentTrajectoryPoint[] | undefined;
    durationSeconds?: number | null;
    onSeek?: (seconds: number) => void;
}) {
    const points = useMemo(
        () => normalizeTrajectory(trajectory, durationSeconds),
        [trajectory, durationSeconds],
    );

    if (points.length === 0) {
        return null;
    }

    const width = 720;
    const height = 140;
    const padX = 24;
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
                            {formatSeconds(p.t)} — sentiment {p.score.toFixed(1)}/10
                        </title>
                    </circle>
                ))}
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
            <p className="mt-1 text-xs text-text-subtle">
                Hover a point for the score. Vocal energy + rep talk-density
                layers slot in here once the upstream signals are wired
                through to the analysis output.
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
