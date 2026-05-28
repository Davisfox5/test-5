"use client";

/**
 * Manager — 10,000-foot view.
 *
 * Replaces /manager-dashboard. Reads precomputed BusinessProfile +
 * playbook_insights as the headline narrative, surfaces live anomaly
 * alerts and a recommendation queue with one-click apply, and keeps the
 * legacy deep signals (training gap) as a drill-down.
 */

import { useMemo, useState } from "react";
import {
    useAcknowledgeAlert,
    useApplyRecommendation,
    useAlertConfig,
    useDismissAlert,
    useDismissRecommendation,
    useManagerAlerts,
    useManagerNarrative,
    useManagerRecommendations,
    useRefreshNarrative,
    useTrainingGap,
    type ManagerAlert,
    type ManagerRecommendation,
    type Severity,
} from "@/lib/manager";

const SEVERITY_BADGE: Record<Severity, string> = {
    high: "bg-error-soft text-error border-error",
    medium: "bg-amber-100 text-amber-700 border-amber-300",
    low: "bg-blue-50 text-blue-700 border-blue-200",
};

const CATEGORY_LABEL: Record<string, string> = {
    coach_rep: "Coach a rep",
    run_campaign: "Run a campaign",
    outreach_at_risk_customer: "Reach out to at-risk customer",
    promote_winning_script: "Promote a winning script",
};

export default function ManagerPage() {
    const [windowDays, setWindowDays] = useState(30);

    return (
        <div className="space-y-8">
            <div className="flex items-baseline justify-between gap-4">
                <h1 className="text-2xl font-bold">Manager</h1>
                <label className="text-sm text-text-muted">
                    Window:{" "}
                    <select
                        value={windowDays}
                        onChange={(e) => setWindowDays(parseInt(e.target.value, 10))}
                        className="ml-1 rounded border border-border bg-bg-card px-2 py-1 text-sm text-text"
                    >
                        <option value={7}>Last 7 days</option>
                        <option value={30}>Last 30 days</option>
                        <option value={90}>Last 90 days</option>
                    </select>
                </label>
            </div>

            <NarrativeCard />
            <AlertsStrip />
            <PlaybookCards />
            <RecommendationsCard />
            <TrainingGapCard windowDays={windowDays} />
        </div>
    );
}

// ─────────────────────────────────────────────────────────────────────
// Narrative
// ─────────────────────────────────────────────────────────────────────

function NarrativeCard() {
    const { data, isLoading } = useManagerNarrative();
    const refresh = useRefreshNarrative();

    if (isLoading) {
        return <Card><p className="text-text-muted">Loading narrative…</p></Card>;
    }
    if (!data) return null;

    const factors = Array.isArray(data.top_factors) ? data.top_factors : [];

    return (
        <Card>
            <div className="flex items-start justify-between gap-4">
                <div className="space-y-2">
                    <div className="text-xs uppercase tracking-wide text-text-subtle">
                        State of the business
                    </div>
                    <p className="text-lg text-text">
                        {data.summary || "Not enough signal yet. The orchestrator hasn't produced a narrative for this tenant."}
                    </p>
                    <div className="flex flex-wrap items-center gap-2 text-xs text-text-subtle">
                        {data.as_of && <span>As of {new Date(data.as_of).toLocaleString()}</span>}
                        {data.confidence !== null && (
                            <span>Confidence: {(data.confidence * 100).toFixed(0)}%</span>
                        )}
                        <span>Version {data.version}</span>
                    </div>
                </div>
                <button
                    onClick={() => refresh.mutate()}
                    disabled={refresh.isPending}
                    className="rounded border border-border bg-bg-card px-3 py-1 text-sm hover:bg-bg-card-hover disabled:opacity-50"
                    title="Force a daily-orchestrator run for this tenant. Rate-limited 1/hr."
                >
                    {refresh.isPending ? "Refreshing…" : "Refresh now"}
                </button>
            </div>
            {factors.length > 0 && (
                <div className="mt-3 flex flex-wrap gap-2">
                    {factors.map((factor, i) => (
                        <FactorChip key={i} factor={factor} />
                    ))}
                </div>
            )}
        </Card>
    );
}

function FactorChip({ factor }: { factor: unknown }) {
    if (typeof factor !== "object" || factor === null) {
        return <Chip>{String(factor)}</Chip>;
    }
    const f = factor as { label?: string; direction?: string; weight?: number };
    const dir = f.direction === "negative" ? "−" : f.direction === "positive" ? "+" : "•";
    return (
        <Chip>
            <span className="mr-1 font-semibold">{dir}</span>
            {f.label || "factor"}
            {typeof f.weight === "number" && (
                <span className="ml-1 text-text-subtle">({f.weight.toFixed(2)})</span>
            )}
        </Chip>
    );
}

// ─────────────────────────────────────────────────────────────────────
// Alerts
// ─────────────────────────────────────────────────────────────────────

function AlertsStrip() {
    const { data: alerts = [], isLoading } = useManagerAlerts({ onlyOpen: true });
    const ack = useAcknowledgeAlert();
    const dismiss = useDismissAlert();

    if (isLoading) return null;

    return (
        <section>
            <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-text-muted">
                Live alerts ({alerts.length})
            </h2>
            {alerts.length === 0 ? (
                <Card>
                    <p className="text-text-muted text-sm">
                        No open alerts. The anomaly scan runs every 15 minutes; any new spike will show up here.
                    </p>
                </Card>
            ) : (
                <div className="grid gap-2 md:grid-cols-2">
                    {alerts.map((a) => (
                        <AlertCard
                            key={a.id}
                            alert={a}
                            onAck={() => ack.mutate(a.id)}
                            onDismiss={() => dismiss.mutate({ id: a.id })}
                        />
                    ))}
                </div>
            )}
        </section>
    );
}

function AlertCard({
    alert,
    onAck,
    onDismiss,
}: {
    alert: ManagerAlert;
    onAck: () => void;
    onDismiss: () => void;
}) {
    return (
        <div className="rounded-lg border border-border bg-bg-card p-3">
            <div className="flex items-center gap-2">
                <span
                    className={`rounded border px-2 py-0.5 text-xs font-semibold uppercase ${SEVERITY_BADGE[alert.severity]}`}
                >
                    {alert.severity}
                </span>
                <span className="text-xs text-text-subtle">{alert.kind.replace("_", " ")}</span>
            </div>
            <p className="mt-2 text-sm font-medium text-text">{alert.title}</p>
            {alert.body && (
                <p className="mt-1 text-xs text-text-muted">{alert.body}</p>
            )}
            <div className="mt-3 flex gap-2">
                <button
                    onClick={onAck}
                    className="rounded border border-border bg-bg-card px-2 py-1 text-xs hover:bg-bg-card-hover"
                >
                    Acknowledge
                </button>
                <button
                    onClick={onDismiss}
                    className="rounded px-2 py-1 text-xs text-text-muted hover:bg-bg-card-hover"
                >
                    Dismiss
                </button>
            </div>
        </div>
    );
}

// ─────────────────────────────────────────────────────────────────────
// Playbook cards — what's working / what's breaking
// ─────────────────────────────────────────────────────────────────────

function PlaybookCards() {
    const { data: narrative } = useManagerNarrative();
    const playbook = (narrative?.playbook_insights || {}) as {
        what_works?: string[];
        what_doesnt?: string[];
        top_performing_phrases?: string[];
        winning_objection_handlers?: string[];
        common_failure_modes?: string[];
    };
    const works = [
        ...(playbook.what_works || []),
        ...(playbook.top_performing_phrases || []),
        ...(playbook.winning_objection_handlers || []),
    ];
    const breaks = [
        ...(playbook.what_doesnt || []),
        ...(playbook.common_failure_modes || []),
    ];

    return (
        <div className="grid gap-4 md:grid-cols-2">
            <Card>
                <h3 className="text-sm font-semibold text-text">What's working</h3>
                {works.length === 0 ? (
                    <p className="mt-2 text-sm text-text-muted">
                        The orchestrator hasn't surfaced reproducible wins yet.
                    </p>
                ) : (
                    <ul className="mt-2 space-y-2 text-sm">
                        {works.slice(0, 6).map((item, i) => (
                            <li key={i} className="rounded border border-border bg-bg p-2">{item}</li>
                        ))}
                    </ul>
                )}
            </Card>
            <Card>
                <h3 className="text-sm font-semibold text-text">What's breaking</h3>
                {breaks.length === 0 ? (
                    <p className="mt-2 text-sm text-text-muted">
                        No persistent failure modes flagged.
                    </p>
                ) : (
                    <ul className="mt-2 space-y-2 text-sm">
                        {breaks.slice(0, 6).map((item, i) => (
                            <li key={i} className="rounded border border-border bg-bg p-2">{item}</li>
                        ))}
                    </ul>
                )}
            </Card>
        </div>
    );
}

// ─────────────────────────────────────────────────────────────────────
// Recommendations
// ─────────────────────────────────────────────────────────────────────

function RecommendationsCard() {
    const { data: recs = [], isLoading } = useManagerRecommendations("open");
    const apply = useApplyRecommendation();
    const dismiss = useDismissRecommendation();

    return (
        <section>
            <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-text-muted">
                Recommended moves
            </h2>
            <Card>
                {isLoading ? (
                    <p className="text-text-muted">Loading recommendations…</p>
                ) : recs.length === 0 ? (
                    <p className="text-text-muted text-sm">
                        No open recommendations. The daily builder runs at 04:30 UTC after the orchestrator.
                    </p>
                ) : (
                    <ul className="divide-y divide-border">
                        {recs.map((rec) => (
                            <RecommendationRow
                                key={rec.id}
                                rec={rec}
                                onApply={() => apply.mutate(rec.id)}
                                onDismiss={() => dismiss.mutate({ id: rec.id })}
                                applying={apply.isPending && apply.variables === rec.id}
                            />
                        ))}
                    </ul>
                )}
            </Card>
        </section>
    );
}

function RecommendationRow({
    rec,
    onApply,
    onDismiss,
    applying,
}: {
    rec: ManagerRecommendation;
    onApply: () => void;
    onDismiss: () => void;
    applying: boolean;
}) {
    const evidence = rec.evidence as Record<string, unknown>;
    const callCount = (evidence?.call_count as number | undefined) ?? null;
    const customerCount = (evidence?.customer_count as number | undefined) ?? null;
    return (
        <li className="py-3">
            <div className="flex items-start justify-between gap-4">
                <div className="space-y-1">
                    <div className="text-xs uppercase tracking-wide text-text-subtle">
                        {CATEGORY_LABEL[rec.category] || rec.category}
                    </div>
                    <p className="text-sm font-medium text-text">{rec.title}</p>
                    {rec.rationale && (
                        <p className="text-xs text-text-muted">{rec.rationale}</p>
                    )}
                    <div className="mt-1 flex flex-wrap gap-2 text-xs text-text-subtle">
                        {callCount !== null && <Chip>{callCount} calls</Chip>}
                        {customerCount !== null && <Chip>{customerCount} customers</Chip>}
                        <Chip>Impact score: {rec.score.toFixed(0)}</Chip>
                    </div>
                </div>
                <div className="flex shrink-0 flex-col gap-2">
                    <button
                        onClick={onApply}
                        disabled={applying}
                        className="rounded bg-primary px-3 py-1 text-xs font-semibold text-bg disabled:opacity-50"
                    >
                        {applying ? "Applying…" : "Apply"}
                    </button>
                    <button
                        onClick={onDismiss}
                        className="rounded px-3 py-1 text-xs text-text-muted hover:bg-bg-card-hover"
                    >
                        Dismiss
                    </button>
                </div>
            </div>
        </li>
    );
}

// ─────────────────────────────────────────────────────────────────────
// Drill-down: training gap (kept from the prior dashboard)
// ─────────────────────────────────────────────────────────────────────

function TrainingGapCard({ windowDays }: { windowDays: number }) {
    const { data, isLoading } = useTrainingGap(windowDays);
    const rows = data?.rows || [];

    return (
        <section>
            <details className="rounded-lg border border-border bg-bg-card">
                <summary className="cursor-pointer select-none px-4 py-3 text-sm font-semibold text-text">
                    Training-gap drill-down (per rep)
                </summary>
                <div className="border-t border-border p-4">
                    {isLoading ? (
                        <p className="text-text-muted text-sm">Loading…</p>
                    ) : rows.length === 0 ? (
                        <p className="text-text-muted text-sm">No reps with calls in the selected window.</p>
                    ) : (
                        <table className="w-full text-sm">
                            <thead className="text-xs uppercase text-text-subtle">
                                <tr>
                                    <th className="text-left">Rep</th>
                                    <th className="text-right">Calls</th>
                                    <th className="text-right">Reflection</th>
                                    <th className="text-right">Open questions</th>
                                    <th className="text-right">Methodology</th>
                                </tr>
                            </thead>
                            <tbody>
                                {rows.map((r) => (
                                    <tr key={r.rep_id ?? "unknown"} className="border-t border-border">
                                        <td>{r.rep_name || "—"}</td>
                                        <td className="text-right">{r.call_count}</td>
                                        <td className="text-right">{pct(r.reflection_rate)}</td>
                                        <td className="text-right">{pct(r.open_question_rate)}</td>
                                        <td className="text-right">{pct(r.avg_methodology_coverage)}</td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    )}
                </div>
            </details>
        </section>
    );
}

function pct(v: number | null) {
    if (v === null || v === undefined) return "—";
    return `${(v * 100).toFixed(0)}%`;
}

// ─────────────────────────────────────────────────────────────────────
// Layout primitives
// ─────────────────────────────────────────────────────────────────────

function Card({ children }: { children: React.ReactNode }) {
    return <div className="rounded-lg border border-border bg-bg-card p-4">{children}</div>;
}

function Chip({ children }: { children: React.ReactNode }) {
    return (
        <span className="inline-flex items-center rounded border border-border bg-bg px-2 py-0.5 text-xs">
            {children}
        </span>
    );
}
