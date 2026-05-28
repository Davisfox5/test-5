"use client";

/**
 * Manager dashboard — aggregate view across the tenant's reps + calls.
 * Manager + admin only; not surfaced in the agent sidebar.
 *
 * v1 ships three aggregations:
 *   - Talk/listen distribution per rep (avg talk_pct + call count)
 *   - Churn-signal throughput (high/medium/low/none counts)
 *   - Methodology adherence (per-framework coverage ratio + most-missed stage)
 *
 * Window defaults to 30 days. Selector lets manager change.
 */

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useApi } from "@/lib/api";

interface RepRow {
    rep_id: string | null;
    rep_name: string | null;
    call_count: number;
    talk_pct_avg: number | null;
}
interface TalkListenDistribution {
    rows: RepRow[];
    tenant_avg_talk_pct: number | null;
}
interface ChurnBucket {
    bucket: string;
    count: number;
}
interface ChurnThroughput {
    window_days: number;
    buckets: ChurnBucket[];
    total_calls: number;
}
interface MethodologyAdherence {
    framework: string;
    total_calls: number;
    avg_coverage_ratio: number | null;
    most_missed_stage: string | null;
}
interface DashboardOverview {
    window_days: number;
    talk_listen: TalkListenDistribution;
    churn_throughput: ChurnThroughput;
    methodology: MethodologyAdherence[];
}

interface RepTrainingGap {
    rep_id: string | null;
    rep_name: string | null;
    call_count: number;
    reflection_rate: number | null;
    open_question_rate: number | null;
    avg_methodology_coverage: number | null;
}

interface TrainingGapReport {
    window_days: number;
    rows: RepTrainingGap[];
}

interface ProductFeedbackTheme {
    theme: string;
    positive_count: number;
    negative_count: number;
    neutral_count: number;
    sample_quote: string | null;
}

export default function ManagerDashboardPage() {
    const api = useApi();
    const [window, setWindow] = useState(30);

    const { data, isLoading, error } = useQuery({
        queryKey: ["manager-dashboard", window],
        queryFn: () =>
            api.get<DashboardOverview>(
                `/manager/dashboard/overview?window_days=${window}`,
            ),
    });

    const trainingGap = useQuery({
        queryKey: ["manager-dashboard", "training-gap", window],
        queryFn: () =>
            api.get<TrainingGapReport>(
                `/manager/dashboard/training-gap?window_days=${window}`,
            ),
    });

    // Product feedback is a manager surface only. Reps don't see this:
    // the feedback came from customer comments and aggregating it pulls
    // across every rep's calls. Period uses the same window selector
    // but the analytics endpoint accepts 7d/30d/90d strings.
    const periodArg = window <= 7 ? "7d" : window <= 30 ? "30d" : "90d";
    const productFeedback = useQuery({
        queryKey: ["product-feedback", periodArg],
        queryFn: () =>
            api.get<ProductFeedbackTheme[]>(
                `/analytics/product-feedback?period=${periodArg}`,
            ),
    });

    return (
        <div className="space-y-6">
            <div className="flex items-baseline justify-between gap-4">
                <h1 className="text-2xl font-bold">Manager dashboard</h1>
                <label className="text-sm text-text-muted">
                    Window:{" "}
                    <select
                        value={window}
                        onChange={(e) => setWindow(parseInt(e.target.value, 10))}
                        className="ml-1 rounded border border-border bg-bg-card px-2 py-1 text-sm text-text"
                    >
                        <option value={7}>Last 7 days</option>
                        <option value={30}>Last 30 days</option>
                        <option value={90}>Last 90 days</option>
                        <option value={180}>Last 180 days</option>
                    </select>
                </label>
            </div>

            {isLoading && (
                <p className="text-sm text-text-subtle">Loading…</p>
            )}
            {error && (
                <p className="text-sm text-accent-rose">
                    Couldn&apos;t load the dashboard. (Manager/admin role
                    required.)
                </p>
            )}

            {data && (
                <div className="grid gap-6 lg:grid-cols-2">
                    <TalkListenCard d={data.talk_listen} />
                    <ChurnThroughputCard d={data.churn_throughput} />
                    <MethodologyCard d={data.methodology} />
                    <ProductFeedbackCard
                        d={productFeedback.data ?? []}
                        loading={productFeedback.isLoading}
                    />
                    <TrainingGapCard
                        d={trainingGap.data?.rows ?? []}
                        loading={trainingGap.isLoading}
                    />
                </div>
            )}
        </div>
    );
}

function TalkListenCard({ d }: { d: TalkListenDistribution }) {
    const tenantAvg = d.tenant_avg_talk_pct;
    return (
        <section className="rounded-lg border border-border bg-bg-card p-5">
            <header className="mb-3 flex items-baseline justify-between">
                <h2 className="text-sm font-semibold">Talk / listen distribution</h2>
                {tenantAvg != null && (
                    <span className="text-xs text-text-subtle">
                        Team avg: {(tenantAvg * 100).toFixed(0)}% rep
                    </span>
                )}
            </header>
            {d.rows.length === 0 ? (
                <p className="text-sm text-text-subtle">No calls in this window.</p>
            ) : (
                <table className="w-full text-sm">
                    <thead>
                        <tr className="text-left text-xs uppercase tracking-wide text-text-subtle">
                            <th className="py-1 pr-2 font-medium">Rep</th>
                            <th className="py-1 pr-2 font-medium">Calls</th>
                            <th className="py-1 pr-2 font-medium">Avg talk %</th>
                        </tr>
                    </thead>
                    <tbody>
                        {d.rows.map((r, i) => (
                            <tr
                                key={r.rep_id ?? i}
                                className="border-t border-border-light"
                            >
                                <td className="py-1.5 pr-2 text-text">
                                    {r.rep_name ?? "Unassigned"}
                                </td>
                                <td className="py-1.5 pr-2 text-text-muted">
                                    {r.call_count}
                                </td>
                                <td className="py-1.5 pr-2 text-text-muted">
                                    {r.talk_pct_avg != null
                                        ? `${(r.talk_pct_avg * 100).toFixed(0)}%`
                                        : "-"}
                                </td>
                            </tr>
                        ))}
                    </tbody>
                </table>
            )}
        </section>
    );
}

function ChurnThroughputCard({ d }: { d: ChurnThroughput }) {
    const order = ["high", "medium", "low", "none"];
    const byBucket: Record<string, number> = {};
    for (const b of d.buckets) byBucket[b.bucket || "none"] = b.count;
    const colors: Record<string, string> = {
        high: "var(--accent-rose)",
        medium: "var(--accent-amber)",
        low: "var(--accent-emerald)",
        none: "var(--text-subtle)",
    };
    return (
        <section className="rounded-lg border border-border bg-bg-card p-5">
            <header className="mb-3 flex items-baseline justify-between">
                <h2 className="text-sm font-semibold">Churn signal throughput</h2>
                <span className="text-xs text-text-subtle">
                    {d.total_calls} call{d.total_calls === 1 ? "" : "s"} ·{" "}
                    {d.window_days}d
                </span>
            </header>
            <ul className="space-y-2 text-sm">
                {order.map((bucket) => {
                    const count = byBucket[bucket] ?? 0;
                    const pct = d.total_calls > 0 ? count / d.total_calls : 0;
                    return (
                        <li key={bucket}>
                            <div className="mb-0.5 flex items-center justify-between">
                                <span className="capitalize text-text">
                                    {bucket}
                                </span>
                                <span className="text-text-muted">
                                    {count} ({(pct * 100).toFixed(0)}%)
                                </span>
                            </div>
                            <div className="h-2 overflow-hidden rounded-full bg-bg-secondary">
                                <div
                                    className="h-full"
                                    style={{
                                        width: `${pct * 100}%`,
                                        backgroundColor: colors[bucket],
                                    }}
                                />
                            </div>
                        </li>
                    );
                })}
            </ul>
        </section>
    );
}

function MethodologyCard({ d }: { d: MethodologyAdherence[] }) {
    return (
        <section className="rounded-lg border border-border bg-bg-card p-5 lg:col-span-2">
            <header className="mb-3">
                <h2 className="text-sm font-semibold">Methodology adherence</h2>
            </header>
            {d.length === 0 ? (
                <p className="text-sm text-text-subtle">
                    No tenant has set a methodology yet, or no analyzed calls
                    fell into one.
                </p>
            ) : (
                <table className="w-full text-sm">
                    <thead>
                        <tr className="text-left text-xs uppercase tracking-wide text-text-subtle">
                            <th className="py-1 pr-2 font-medium">Framework</th>
                            <th className="py-1 pr-2 font-medium">Calls</th>
                            <th className="py-1 pr-2 font-medium">
                                Avg coverage
                            </th>
                            <th className="py-1 pr-2 font-medium">
                                Most missed
                            </th>
                        </tr>
                    </thead>
                    <tbody>
                        {d.map((m) => (
                            <tr
                                key={m.framework}
                                className="border-t border-border-light"
                            >
                                <td className="py-1.5 pr-2 capitalize text-text">
                                    {m.framework}
                                </td>
                                <td className="py-1.5 pr-2 text-text-muted">
                                    {m.total_calls}
                                </td>
                                <td className="py-1.5 pr-2 text-text-muted">
                                    {m.avg_coverage_ratio != null
                                        ? `${(m.avg_coverage_ratio * 100).toFixed(0)}%`
                                        : "-"}
                                </td>
                                <td className="py-1.5 pr-2 capitalize text-text-muted">
                                    {m.most_missed_stage?.replace(/_/g, " ") ??
                                        "-"}
                                </td>
                            </tr>
                        ))}
                    </tbody>
                </table>
            )}
        </section>
    );
}

function ProductFeedbackCard({
    d,
    loading,
}: {
    d: ProductFeedbackTheme[];
    loading: boolean;
}) {
    return (
        <section className="rounded-lg border border-border bg-bg-card p-5 lg:col-span-2">
            <header className="mb-3 flex items-baseline justify-between">
                <div>
                    <h2 className="text-sm font-semibold">
                        Product feedback themes
                    </h2>
                    <p className="text-xs text-text-subtle">
                        Customer-side feedback Linda extracted across the team.
                        Manager-only. keeps reps from drowning in noise on
                        their own pages.
                    </p>
                </div>
            </header>
            {loading ? (
                <p className="text-sm text-text-subtle">Loading…</p>
            ) : d.length === 0 ? (
                <p className="text-sm text-text-subtle">
                    No themes surfaced in this window.
                </p>
            ) : (
                <ul className="space-y-3">
                    {d.map((t) => {
                        const total =
                            t.positive_count + t.negative_count + t.neutral_count;
                        const negPct =
                            total > 0 ? (t.negative_count / total) * 100 : 0;
                        const posPct =
                            total > 0 ? (t.positive_count / total) * 100 : 0;
                        return (
                            <li
                                key={t.theme}
                                className="rounded-md border border-border-light bg-bg-secondary p-3"
                            >
                                <div className="flex flex-wrap items-baseline justify-between gap-2">
                                    <h3 className="text-sm font-medium text-text">
                                        {t.theme}
                                    </h3>
                                    <span className="text-xs text-text-muted">
                                        {total} mention{total === 1 ? "" : "s"}
                                    </span>
                                </div>
                                <div className="mt-2 flex h-2 overflow-hidden rounded-full bg-bg-card">
                                    <div
                                        className="bg-accent-emerald"
                                        style={{ width: `${posPct}%` }}
                                    />
                                    <div
                                        className="bg-accent-rose"
                                        style={{ width: `${negPct}%` }}
                                    />
                                </div>
                                <div className="mt-1 flex gap-4 text-[11px] text-text-subtle">
                                    <span>👍 {t.positive_count}</span>
                                    <span>👎 {t.negative_count}</span>
                                    <span>· {t.neutral_count} neutral</span>
                                </div>
                                {t.sample_quote ? (
                                    <p className="mt-2 line-clamp-2 text-xs italic text-text-muted">
                                        &ldquo;{t.sample_quote}&rdquo;
                                    </p>
                                ) : null}
                            </li>
                        );
                    })}
                </ul>
            )}
        </section>
    );
}

function TrainingGapCard({
    d,
    loading,
}: {
    d: RepTrainingGap[];
    loading: boolean;
}) {
    return (
        <section className="rounded-lg border border-border bg-bg-card p-5 lg:col-span-2">
            <header className="mb-3 flex items-baseline justify-between">
                <div>
                    <h2 className="text-sm font-semibold">
                        Training-gap report
                    </h2>
                    <p className="text-xs text-text-subtle">
                        Per-rep reflection rate, open-question rate, and
                        methodology coverage.
                    </p>
                </div>
            </header>
            {loading ? (
                <p className="text-sm text-text-subtle">Loading…</p>
            ) : d.length === 0 ? (
                <p className="text-sm text-text-subtle">
                    No analyzed calls in this window.
                </p>
            ) : (
                <table className="w-full text-sm">
                    <thead>
                        <tr className="text-left text-xs uppercase tracking-wide text-text-subtle">
                            <th className="py-1 pr-2 font-medium">Rep</th>
                            <th className="py-1 pr-2 font-medium">Calls</th>
                            <th className="py-1 pr-2 font-medium">
                                Reflection
                            </th>
                            <th className="py-1 pr-2 font-medium">
                                Open Qs
                            </th>
                            <th className="py-1 pr-2 font-medium">
                                Methodology
                            </th>
                        </tr>
                    </thead>
                    <tbody>
                        {d.map((r, i) => (
                            <tr
                                key={r.rep_id ?? i}
                                className="border-t border-border-light"
                            >
                                <td className="py-1.5 pr-2 text-text">
                                    {r.rep_name ?? "Unassigned"}
                                </td>
                                <td className="py-1.5 pr-2 text-text-muted">
                                    {r.call_count}
                                </td>
                                <td className="py-1.5 pr-2 text-text-muted">
                                    {pct(r.reflection_rate)}
                                </td>
                                <td className="py-1.5 pr-2 text-text-muted">
                                    {pct(r.open_question_rate)}
                                </td>
                                <td className="py-1.5 pr-2 text-text-muted">
                                    {pct(r.avg_methodology_coverage)}
                                </td>
                            </tr>
                        ))}
                    </tbody>
                </table>
            )}
        </section>
    );
}

function pct(v: number | null): string {
    if (v == null) return "-";
    return `${(v * 100).toFixed(0)}%`;
}
