"use client";

import { useMemo } from "react";

export interface DailyPoint {
    date: string;
    interaction_count: number;
    avg_sentiment: number | null;
}

export interface TrendsChartProps {
    points: DailyPoint[];
    /** Optional QA-score series (0–100, by date). */
    qaByDate?: Record<string, number>;
    /** Optional rapport series (0–1, by date). */
    rapportByDate?: Record<string, number>;
    height?: number;
}

/**
 * Combined trends chart for the dashboard.
 *
 * Bars: interaction count per day (left axis, raw counts).
 * Lines: sentiment (0–10), QA score (0–100), rapport (0–1) — all
 * rescaled to a shared 0–100 right-axis so a single chart can carry
 * all three. The legend labels each line with its native unit so
 * users still see the un-rescaled value in tooltips.
 *
 * Pure SVG, no chart-lib dependency — keeps the bundle lean and
 * matches CallDynamicsChart's in-house SVG style.
 */
export function TrendsChart({
    points,
    qaByDate,
    rapportByDate,
    height = 220,
}: TrendsChartProps) {
    const layout = useMemo(() => {
        if (points.length === 0) return null;

        const sorted = [...points].sort((a, b) => a.date.localeCompare(b.date));

        const maxCount = Math.max(1, ...sorted.map((p) => p.interaction_count));
        const width = 100; // viewBox is 0..100; CSS scales to container
        const padX = 5;
        const padTop = 4;
        const padBottom = 14;
        const innerW = width - padX * 2;
        const innerH = height - padTop - padBottom;

        const xFor = (i: number) =>
            padX +
            (sorted.length === 1 ? innerW / 2 : (i / (sorted.length - 1)) * innerW);
        const yForCount = (n: number) =>
            padTop + innerH - (n / maxCount) * innerH;
        const yForPct = (pct: number) =>
            padTop + innerH - (pct / 100) * innerH;

        // Build line series (only days with values). Convert to 0–100.
        const sentimentLine: Array<{ x: number; y: number; raw: number }> = [];
        const qaLine: Array<{ x: number; y: number; raw: number }> = [];
        const rapportLine: Array<{ x: number; y: number; raw: number }> = [];
        for (let i = 0; i < sorted.length; i++) {
            const p = sorted[i];
            const x = xFor(i);
            if (p.avg_sentiment != null) {
                const pct = (p.avg_sentiment / 10) * 100;
                sentimentLine.push({ x, y: yForPct(pct), raw: p.avg_sentiment });
            }
            const qa = qaByDate?.[p.date];
            if (qa != null) qaLine.push({ x, y: yForPct(qa), raw: qa });
            const rap = rapportByDate?.[p.date];
            if (rap != null)
                rapportLine.push({ x, y: yForPct(rap * 100), raw: rap });
        }

        const linePath = (
            pts: Array<{ x: number; y: number }>,
        ): string =>
            pts.length === 0
                ? ""
                : pts
                      .map((p, idx) => `${idx === 0 ? "M" : "L"}${p.x} ${p.y}`)
                      .join(" ");

        // Bars — slightly inset from each x position. Width is a fixed
        // fraction of the per-day slot so dense periods still have gaps.
        const slotW = sorted.length > 1 ? innerW / (sorted.length - 1) : innerW;
        const barW = Math.min(6, Math.max(1.2, slotW * 0.55));

        return {
            sorted,
            width,
            height,
            padX,
            padTop,
            padBottom,
            innerH,
            xFor,
            yForCount,
            yForPct,
            barW,
            maxCount,
            sentimentLine,
            qaLine,
            rapportLine,
            linePath,
        };
    }, [points, qaByDate, rapportByDate, height]);

    if (!layout) {
        return (
            <div
                className="flex items-center justify-center rounded-md border border-dashed border-border text-sm text-text-subtle"
                style={{ height }}
            >
                No trend data yet
            </div>
        );
    }

    const {
        sorted,
        width,
        padX,
        padTop,
        innerH,
        xFor,
        yForCount,
        yForPct,
        barW,
        maxCount,
        sentimentLine,
        qaLine,
        rapportLine,
        linePath,
    } = layout;

    // Pick ~5 evenly-spaced x-axis labels.
    const tickIdxs: number[] = [];
    const tickCount = Math.min(5, sorted.length);
    for (let t = 0; t < tickCount; t++) {
        tickIdxs.push(Math.round((t / Math.max(1, tickCount - 1)) * (sorted.length - 1)));
    }

    return (
        <div className="relative">
            <svg
                viewBox={`0 0 ${width} ${height}`}
                preserveAspectRatio="none"
                className="block w-full"
                style={{ height }}
                role="img"
                aria-label="Trends over time"
            >
                {/* Y gridlines at 25/50/75/100% */}
                {[0, 0.25, 0.5, 0.75, 1].map((f) => (
                    <line
                        key={f}
                        x1={padX}
                        x2={width - padX}
                        y1={yForPct(f * 100)}
                        y2={yForPct(f * 100)}
                        stroke="var(--border)"
                        strokeWidth={0.2}
                        strokeDasharray={f === 0 || f === 1 ? "" : "1 1"}
                    />
                ))}

                {/* Bars (call counts) */}
                {sorted.map((p, i) => {
                    const x = xFor(i);
                    const y = yForCount(p.interaction_count);
                    const h = padTop + innerH - y;
                    if (h <= 0) return null;
                    return (
                        <rect
                            key={p.date}
                            x={x - barW / 2}
                            y={y}
                            width={barW}
                            height={h}
                            fill="var(--primary)"
                            opacity={0.18}
                        >
                            <title>{`${p.date}: ${p.interaction_count} call${p.interaction_count === 1 ? "" : "s"}`}</title>
                        </rect>
                    );
                })}

                {/* Sentiment line (0-10 → rescaled) */}
                {sentimentLine.length > 1 && (
                    <path
                        d={linePath(sentimentLine)}
                        fill="none"
                        stroke="var(--accent-emerald)"
                        strokeWidth={1}
                        vectorEffect="non-scaling-stroke"
                    />
                )}
                {sentimentLine.map((pt, i) => (
                    <circle
                        key={`s${i}`}
                        cx={pt.x}
                        cy={pt.y}
                        r={1.2}
                        fill="var(--accent-emerald)"
                    >
                        <title>{`Sentiment: ${pt.raw.toFixed(1)}/10`}</title>
                    </circle>
                ))}

                {/* QA line (0-100) */}
                {qaLine.length > 1 && (
                    <path
                        d={linePath(qaLine)}
                        fill="none"
                        stroke="var(--accent-cyan)"
                        strokeWidth={1}
                        vectorEffect="non-scaling-stroke"
                    />
                )}
                {qaLine.map((pt, i) => (
                    <circle
                        key={`q${i}`}
                        cx={pt.x}
                        cy={pt.y}
                        r={1.2}
                        fill="var(--accent-cyan)"
                    >
                        <title>{`QA: ${pt.raw.toFixed(1)}/100`}</title>
                    </circle>
                ))}

                {/* Rapport line (0-1 → rescaled) */}
                {rapportLine.length > 1 && (
                    <path
                        d={linePath(rapportLine)}
                        fill="none"
                        stroke="var(--accent-amber)"
                        strokeWidth={1}
                        vectorEffect="non-scaling-stroke"
                    />
                )}
                {rapportLine.map((pt, i) => (
                    <circle
                        key={`r${i}`}
                        cx={pt.x}
                        cy={pt.y}
                        r={1.2}
                        fill="var(--accent-amber)"
                    >
                        <title>{`Rapport (LSM): ${pt.raw.toFixed(2)}`}</title>
                    </circle>
                ))}

                {/* X-axis ticks */}
                {tickIdxs.map((i) => (
                    <text
                        key={`xt${i}`}
                        x={xFor(i)}
                        y={padTop + innerH + 8}
                        fontSize={3}
                        textAnchor="middle"
                        fill="var(--text-subtle)"
                    >
                        {sorted[i]?.date.slice(5)}
                    </text>
                ))}
            </svg>
            <div className="mt-2 flex flex-wrap items-center gap-4 text-xs text-text-subtle">
                <LegendSwatch color="var(--primary)" opacity={0.4}>
                    Calls (max {maxCount})
                </LegendSwatch>
                {sentimentLine.length > 0 && (
                    <LegendSwatch color="var(--accent-emerald)">
                        Sentiment / 10
                    </LegendSwatch>
                )}
                {qaLine.length > 0 && (
                    <LegendSwatch color="var(--accent-cyan)">
                        QA / 100
                    </LegendSwatch>
                )}
                {rapportLine.length > 0 && (
                    <LegendSwatch color="var(--accent-amber)">
                        Rapport (LSM)
                    </LegendSwatch>
                )}
            </div>
        </div>
    );
}

function LegendSwatch({
    color,
    opacity = 1,
    children,
}: {
    color: string;
    opacity?: number;
    children: React.ReactNode;
}) {
    return (
        <span className="inline-flex items-center gap-1.5">
            <span
                className="inline-block h-2 w-3 rounded-sm"
                style={{ background: color, opacity }}
            />
            <span>{children}</span>
        </span>
    );
}
