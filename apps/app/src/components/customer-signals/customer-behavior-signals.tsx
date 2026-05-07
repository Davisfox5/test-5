"use client";

import {
    type BehaviorRadarValues,
    type ChangeReadinessOutput,
    useCustomerBehaviorSignals,
} from "@/lib/customer-signals";

/**
 * Customer Behavior Signals — combines the 6-axis Behavior Radar
 * and the Change-Readiness Index into one block for the customer
 * detail page. Pulls aggregated signals across every analyzed
 * interaction on this customer.
 */
export function CustomerBehaviorSignals({ customerId }: { customerId: string }) {
    const { data, isLoading, error } = useCustomerBehaviorSignals(customerId);

    if (isLoading) {
        return (
            <div className="rounded-lg border border-border bg-bg-card p-5">
                <p className="text-sm text-text-subtle">Loading signals…</p>
            </div>
        );
    }
    if (error || !data) {
        return (
            <div className="rounded-lg border border-border bg-bg-card p-5">
                <p className="text-sm text-text-subtle">
                    No behavior signals yet — they accrue as calls are analyzed.
                </p>
            </div>
        );
    }

    if (data.source_interaction_count === 0) {
        return (
            <div className="rounded-lg border border-border bg-bg-card p-5">
                <h3 className="text-sm font-semibold">Customer behavior</h3>
                <p className="mt-2 text-sm text-text-muted">
                    No analyzed calls yet. Behavior radar + change-readiness
                    score appear once at least one call has been processed.
                </p>
            </div>
        );
    }

    return (
        <section className="rounded-lg border border-border bg-bg-card p-5">
            <header className="mb-3 flex items-baseline justify-between gap-3">
                <h3 className="text-sm font-semibold">Customer behavior</h3>
                <span className="text-xs text-text-subtle">
                    Across {data.source_interaction_count} call
                    {data.source_interaction_count === 1 ? "" : "s"} ·{" "}
                    {data.signal_density} quote
                    {data.signal_density === 1 ? "" : "s"}
                </span>
            </header>
            <div className="grid gap-5 lg:grid-cols-2">
                <BehaviorRadar values={data.radar} />
                <ChangeReadinessGauge readiness={data.change_readiness} />
            </div>
        </section>
    );
}

// ── Radar chart ─────────────────────────────────────────────────────────

const AXES: Array<{ key: keyof BehaviorRadarValues; label: string; positive: boolean }> = [
    { key: "commitment", label: "Commitment", positive: true },
    { key: "openness", label: "Openness", positive: true },
    { key: "engagement", label: "Engagement", positive: true },
    { key: "trust", label: "Trust", positive: true },
    { key: "decision_urgency", label: "Urgency", positive: true },
    // Friction is "lower is better" — show the inverted value on the
    // radar so a healthy customer reads as a uniformly large polygon.
    { key: "friction", label: "Friction", positive: false },
];

function BehaviorRadar({ values }: { values: BehaviorRadarValues }) {
    const size = 240;
    const cx = size / 2;
    const cy = size / 2;
    const r = size / 2 - 36; // padding for labels
    const levels = [0.2, 0.4, 0.6, 0.8, 1.0];

    const points = AXES.map((axis, i) => {
        const angle = (Math.PI * 2 * i) / AXES.length - Math.PI / 2;
        const raw = values[axis.key] ?? 0;
        // Friction polygon shows the inverted axis so the visual
        // "bigger = better" rule holds across all six.
        const value = axis.positive ? raw : 1 - raw;
        const x = cx + Math.cos(angle) * r * value;
        const y = cy + Math.sin(angle) * r * value;
        return { x, y, angle, label: axis.label };
    });

    const path = points.map((p, i) => `${i === 0 ? "M" : "L"}${p.x},${p.y}`).join(" ") + " Z";

    return (
        <div className="flex flex-col items-center">
            <svg
                width={size}
                height={size}
                viewBox={`0 0 ${size} ${size}`}
                role="img"
                aria-label="Customer behavior radar"
            >
                {/* Background grid — concentric polygons. */}
                {levels.map((level) => (
                    <polygon
                        key={level}
                        points={AXES.map((_, i) => {
                            const angle = (Math.PI * 2 * i) / AXES.length - Math.PI / 2;
                            const x = cx + Math.cos(angle) * r * level;
                            const y = cy + Math.sin(angle) * r * level;
                            return `${x},${y}`;
                        }).join(" ")}
                        fill="none"
                        stroke="var(--border-light)"
                        strokeWidth={1}
                    />
                ))}
                {/* Spokes. */}
                {AXES.map((_, i) => {
                    const angle = (Math.PI * 2 * i) / AXES.length - Math.PI / 2;
                    return (
                        <line
                            key={i}
                            x1={cx}
                            y1={cy}
                            x2={cx + Math.cos(angle) * r}
                            y2={cy + Math.sin(angle) * r}
                            stroke="var(--border-light)"
                            strokeWidth={1}
                        />
                    );
                })}
                {/* Actual values. */}
                <path
                    d={path}
                    fill="var(--primary-soft)"
                    stroke="var(--primary)"
                    strokeWidth={1.5}
                    fillOpacity={0.5}
                />
                {/* Labels — placed slightly outside each axis tip. */}
                {AXES.map((axis, i) => {
                    const angle = (Math.PI * 2 * i) / AXES.length - Math.PI / 2;
                    const lx = cx + Math.cos(angle) * (r + 18);
                    const ly = cy + Math.sin(angle) * (r + 18);
                    return (
                        <text
                            key={axis.key}
                            x={lx}
                            y={ly}
                            fontSize={11}
                            fill="var(--text-muted)"
                            textAnchor="middle"
                            dominantBaseline="middle"
                        >
                            {axis.label}
                        </text>
                    );
                })}
            </svg>
            <p className="mt-1 max-w-xs text-center text-xs text-text-subtle">
                Each axis is a research-backed customer signal. Friction is
                inverted so a healthy customer fills the polygon.
            </p>
        </div>
    );
}

// ── Change-Readiness gauge ──────────────────────────────────────────────

function ChangeReadinessGauge({ readiness }: { readiness: ChangeReadinessOutput }) {
    const score = Math.max(0, Math.min(100, readiness.score));
    const confidence = readiness.confidence;
    const ringColor =
        score >= 70
            ? "var(--accent-emerald)"
            : score >= 40
                ? "var(--accent-amber)"
                : "var(--accent-rose)";

    // Top contributing factors — sorted by absolute magnitude so a
    // strong friction penalty surfaces alongside positive drivers.
    const contributors = Object.entries(readiness.contributing || {})
        .sort((a, b) => Math.abs(b[1]) - Math.abs(a[1]))
        .slice(0, 4);

    return (
        <div className="flex flex-col items-center justify-center">
            <div
                className="flex h-28 w-28 items-center justify-center rounded-full border-4"
                style={{ borderColor: ringColor }}
            >
                <div className="text-center">
                    <div className="text-3xl font-bold text-text">{score}</div>
                    <div className="text-[10px] uppercase tracking-wide text-text-subtle">
                        of 100
                    </div>
                </div>
            </div>
            <div className="mt-2 text-sm font-medium text-text">
                Change-Readiness
            </div>
            <div className="text-xs capitalize text-text-muted">
                {confidence} confidence
            </div>
            {contributors.length > 0 && (
                <ul className="mt-3 w-full max-w-xs space-y-1 text-xs">
                    {contributors.map(([key, value]) => (
                        <li key={key} className="flex items-center justify-between gap-2">
                            <span className="capitalize text-text-muted">
                                {key.replace(/_/g, " ")}
                            </span>
                            <span
                                className={
                                    value < 0
                                        ? "text-accent-rose"
                                        : "text-text"
                                }
                            >
                                {value >= 0 ? "+" : ""}
                                {value.toFixed(2)}
                            </span>
                        </li>
                    ))}
                </ul>
            )}
        </div>
    );
}
