"use client";

import { useState } from "react";
import { useMe } from "@/lib/me";
import {
    AnalyticsPeriod,
    useBusinessHealth,
    useCoachingInsights,
    useTeamStats,
    useTopicsTrend,
} from "@/lib/analytics";
import {
    ErrorCard,
    ManagerGate,
    Section,
    SkeletonCard,
    humanizeError,
} from "@/components/admin/section";

const PERIODS: AnalyticsPeriod[] = ["7d", "30d", "90d"];

export default function AnalyticsPage() {
    const { data: me } = useMe();
    const role = me?.user?.role;
    const isSandbox = me?.tenant.plan_tier === "sandbox";
    const [period, setPeriod] = useState<AnalyticsPeriod>("30d");

    const business = useBusinessHealth(period);
    const team = useTeamStats();
    const topics = useTopicsTrend(period);
    const coaching = useCoachingInsights(period);

    const sandboxEmpty =
        isSandbox &&
        (business.data?.total_interactions ?? 0) === 0 &&
        (team.data?.length ?? 0) === 0;

    return (
        <div className="space-y-6">
            <header className="flex items-start justify-between gap-4">
                <div>
                    <h2 className="text-2xl font-bold">Analytics</h2>
                    <p className="text-text-muted mt-1">
                        Roll-ups across {me?.tenant.name ?? "your tenant"}'s
                        interactions.
                    </p>
                </div>
                <div className="flex gap-1 rounded-md border border-border bg-bg-raised p-1">
                    {PERIODS.map((p) => (
                        <button
                            key={p}
                            type="button"
                            onClick={() => setPeriod(p)}
                            className={`rounded-md px-3 py-1 text-xs font-medium ${
                                period === p
                                    ? "bg-primary text-white"
                                    : "text-text-muted hover:text-text-main"
                            }`}
                        >
                            {p}
                        </button>
                    ))}
                </div>
            </header>

            <ManagerGate role={role}>
                {sandboxEmpty ? (
                    <Section title="Sandbox tenant">
                        <p className="text-sm text-text-muted">
                            No interactions yet — analytics light up once
                            calls and emails start flowing through your
                            workspace. Use the seed-data button on the
                            dashboard if you want demo numbers.
                        </p>
                    </Section>
                ) : null}

                <Section
                    title="Business health"
                    subtitle="Headline numbers from your interaction stream."
                >
                    {business.error ? (
                        <ErrorCard
                            message={humanizeError(business.error)}
                        />
                    ) : business.isLoading ? (
                        <SkeletonCard />
                    ) : business.data ? (
                        <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
                            <Stat
                                label="Health score"
                                value={`${business.data.health_score}`}
                                suffix="/ 100"
                            />
                            <Stat
                                label="Interactions"
                                value={business.data.total_interactions.toString()}
                            />
                            <Stat
                                label="Avg sentiment"
                                value={
                                    business.data.avg_sentiment != null
                                        ? business.data.avg_sentiment.toFixed(
                                              2,
                                          )
                                        : "—"
                                }
                                suffix={
                                    business.data.avg_sentiment != null
                                        ? "/ 10"
                                        : ""
                                }
                            />
                            <Stat
                                label="Channels"
                                value={business.data.channels_breakdown.length.toString()}
                            />
                        </div>
                    ) : null}

                    {business.data?.channels_breakdown.length ? (
                        <ul className="mt-4 grid grid-cols-1 gap-2 sm:grid-cols-2">
                            {business.data.channels_breakdown.map((c) => (
                                <li
                                    key={c.channel}
                                    className="flex items-center justify-between rounded-md border border-border bg-bg-raised px-3 py-2 text-xs"
                                >
                                    <span className="capitalize">
                                        {c.channel}
                                    </span>
                                    <span className="text-text-muted">
                                        {c.count} ·{" "}
                                        {c.avg_sentiment != null
                                            ? c.avg_sentiment.toFixed(1)
                                            : "—"}
                                    </span>
                                </li>
                            ))}
                        </ul>
                    ) : null}
                </Section>

                <Section
                    title="Team performance"
                    subtitle="Per-agent volume; bar widths scaled to the team max."
                >
                    {team.error ? (
                        <ErrorCard message={humanizeError(team.error)} />
                    ) : team.isLoading ? (
                        <SkeletonCard />
                    ) : !team.data?.length ? (
                        <p className="text-sm text-text-muted">
                            No agent activity yet for this tenant.
                        </p>
                    ) : (
                        <TeamBars stats={team.data} />
                    )}
                </Section>

                <Section
                    title="Topic trends"
                    subtitle="Top mentions in this period vs. the previous equal window."
                >
                    {topics.error ? (
                        <ErrorCard message={humanizeError(topics.error)} />
                    ) : topics.isLoading ? (
                        <SkeletonCard />
                    ) : !topics.data?.length ? (
                        <p className="text-sm text-text-muted">
                            No topic data extracted yet.
                        </p>
                    ) : (
                        <ul className="space-y-1">
                            {topics.data.slice(0, 10).map((t) => (
                                <li
                                    key={t.name}
                                    className="flex items-center justify-between rounded-md border border-border bg-bg-raised px-3 py-2 text-xs"
                                >
                                    <span>{t.name}</span>
                                    <span className="flex items-center gap-3">
                                        <span className="text-text-muted">
                                            {t.mentions} mentions
                                        </span>
                                        <DeltaBadge
                                            value={t.pct_change ?? null}
                                        />
                                    </span>
                                </li>
                            ))}
                        </ul>
                    )}
                </Section>

                <Section
                    title="Coaching insights"
                    subtitle="Where reps are landing well and where they're slipping."
                >
                    {coaching.error ? (
                        <ErrorCard
                            message={humanizeError(coaching.error)}
                        />
                    ) : coaching.isLoading ? (
                        <SkeletonCard />
                    ) : coaching.data ? (
                        <div className="space-y-4">
                            <div>
                                <p className="text-xs uppercase tracking-wide text-text-subtle">
                                    Avg script adherence
                                </p>
                                <p className="text-xl font-semibold">
                                    {coaching.data.avg_script_adherence != null
                                        ? `${(
                                              coaching.data
                                                  .avg_script_adherence * 100
                                          ).toFixed(0)}%`
                                        : "—"}
                                </p>
                            </div>
                            <CoachingList
                                title="What went well"
                                items={coaching.data.top_strengths}
                                tone="emerald"
                            />
                            <CoachingList
                                title="Top improvements"
                                items={coaching.data.top_improvements}
                                tone="amber"
                            />
                            <CoachingList
                                title="Compliance gaps"
                                items={coaching.data.top_compliance_gaps}
                                tone="rose"
                            />
                        </div>
                    ) : null}
                </Section>
            </ManagerGate>
        </div>
    );
}

function Stat({
    label,
    value,
    suffix,
}: {
    label: string;
    value: string;
    suffix?: string;
}) {
    return (
        <div className="rounded-md border border-border bg-bg-raised p-3">
            <p className="text-xs uppercase tracking-wide text-text-subtle">
                {label}
            </p>
            <p className="mt-1 text-xl font-semibold">
                {value}
                {suffix ? (
                    <span className="ml-1 text-xs text-text-muted">
                        {suffix}
                    </span>
                ) : null}
            </p>
        </div>
    );
}

function DeltaBadge({ value }: { value: number | null }) {
    if (value == null)
        return <span className="text-text-subtle">—</span>;
    const tone =
        value > 5
            ? "text-accent-emerald"
            : value < -5
              ? "text-accent-rose"
              : "text-text-muted";
    const sign = value > 0 ? "+" : "";
    return (
        <span className={`tabular-nums ${tone}`}>
            {sign}
            {value.toFixed(1)}%
        </span>
    );
}

function TeamBars({
    stats,
}: {
    stats: Array<{
        agent_id: string;
        name: string | null;
        interaction_count: number;
        avg_sentiment: number | null;
    }>;
}) {
    const max = Math.max(1, ...stats.map((s) => s.interaction_count));
    const width = 320;
    const barH = 18;
    const gap = 8;
    const labelWidth = 140;
    const totalH = stats.length * (barH + gap);
    return (
        <svg
            viewBox={`0 0 ${labelWidth + width + 60} ${totalH}`}
            className="w-full"
            role="img"
            aria-label="Per-agent interaction volume"
        >
            {stats.map((s, i) => {
                const y = i * (barH + gap);
                const w = (s.interaction_count / max) * width;
                return (
                    <g key={s.agent_id}>
                        <text
                            x={0}
                            y={y + barH * 0.7}
                            className="fill-text-muted"
                            fontSize="11"
                        >
                            {(s.name ?? "—").slice(0, 18)}
                        </text>
                        <rect
                            x={labelWidth}
                            y={y}
                            width={Math.max(2, w)}
                            height={barH}
                            className="fill-primary"
                            rx={3}
                        />
                        <text
                            x={labelWidth + Math.max(2, w) + 6}
                            y={y + barH * 0.7}
                            className="fill-text-subtle"
                            fontSize="11"
                        >
                            {s.interaction_count}
                            {s.avg_sentiment != null
                                ? ` · ${s.avg_sentiment.toFixed(1)}`
                                : ""}
                        </text>
                    </g>
                );
            })}
        </svg>
    );
}

function CoachingList({
    title,
    items,
    tone,
}: {
    title: string;
    items: Array<{ text: string; count: number }>;
    tone: "emerald" | "amber" | "rose";
}) {
    const dotClass =
        tone === "emerald"
            ? "bg-accent-emerald"
            : tone === "amber"
              ? "bg-accent-amber"
              : "bg-accent-rose";
    if (!items.length) return null;
    return (
        <div>
            <p className="text-xs uppercase tracking-wide text-text-subtle mb-2">
                {title}
            </p>
            <ul className="space-y-1">
                {items.slice(0, 5).map((it, idx) => (
                    <li
                        key={`${title}-${idx}`}
                        className="flex items-start gap-2 text-sm"
                    >
                        <span
                            className={`mt-1.5 inline-block h-1.5 w-1.5 rounded-full ${dotClass}`}
                        />
                        <span className="flex-1">{it.text}</span>
                        <span className="text-xs text-text-subtle">
                            ×{it.count}
                        </span>
                    </li>
                ))}
            </ul>
        </div>
    );
}
