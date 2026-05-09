"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import { useMe } from "@/lib/me";
import {
    formatRelative,
    sentimentLabel,
    useDashboardSummary,
    useInteractions,
    useOpenActionItems,
    type ActionItemOut,
    type DashboardPeriod,
    type InteractionOut,
} from "@/lib/interactions";
import {
    useBusinessHealth,
    useCoachingInsights,
    useTrends,
    useSignals,
    useAccountHealth,
    useManagerOverview,
    type AccountHealthRow,
} from "@/lib/analytics";
import { useNotifications } from "@/lib/notifications";
import { useOAuthStatus } from "@/lib/oauth";
import { TrendsChart } from "@/components/dashboard/trends-chart";

const PERIODS: { value: DashboardPeriod; label: string }[] = [
    { value: "7d", label: "7d" },
    { value: "14d", label: "14d" },
    { value: "30d", label: "30d" },
    { value: "60d", label: "60d" },
    { value: "90d", label: "90d" },
];

export default function DashboardPage() {
    const me = useMe();
    const [period, setPeriod] = useState<DashboardPeriod>("30d");
    const summary = useDashboardSummary(period);
    const trends = useTrends(period);
    const business = useBusinessHealth(period);
    const coaching = useCoachingInsights(period);
    const signals = useSignals(period);
    const accounts = useAccountHealth(30, 5);
    const isManager =
        me.data?.user?.role === "manager" || me.data?.user?.role === "admin";
    // Skip the manager-only endpoint for agents so we don't burn a 403
    // on every page load. The flag flips once ``me`` resolves.
    const manager = useManagerOverview(30, isManager);
    const recent = useInteractions(
        { limit: 5 },
        {
            refetchInterval: 60_000,
            refetchOnWindowFocus: true,
        },
    );
    const actionItems = useOpenActionItems(5);
    const notifs = useNotifications();
    const oauth = useOAuthStatus();

    const qaByDate = useMemo(() => {
        const out: Record<string, number> = {};
        for (const r of trends.data ?? []) {
            // Backend emits one supplemental row per day with channel=null
            // carrying QA + rapport rollups.
            if (r.channel == null && r.avg_qa_score != null) {
                out[r.date] = r.avg_qa_score;
            }
        }
        return out;
    }, [trends.data]);
    const rapportByDate = useMemo(() => {
        const out: Record<string, number> = {};
        for (const r of trends.data ?? []) {
            if (r.channel == null && r.avg_rapport != null) {
                out[r.date] = r.avg_rapport;
            }
        }
        return out;
    }, [trends.data]);

    // Aggregate trend points across channels per date so the chart has
    // one bar per day rather than one bar per channel-day. Sentiment is
    // averaged weighted by per-channel call count. MUST stay above the
    // early-return guards so the hook count is stable across renders.
    const trendPoints = useMemoTrendPoints(trends.data);

    if (me.isLoading) return <p className="text-text-muted">Loading…</p>;
    if (me.error || !me.data)
        return (
            <p className="text-accent-rose">Couldn&apos;t load your tenant.</p>
        );

    const { tenant, user } = me.data;
    const firstName = user?.name?.split(" ")[0];
    const isEmpty = !recent.isLoading && (recent.data?.length ?? 0) === 0;

    return (
        <div className="space-y-6">
            <header className="flex flex-wrap items-start justify-between gap-3">
                <div>
                    <h2 className="text-2xl font-bold">
                        Welcome back{firstName ? `, ${firstName}` : ""}.
                    </h2>
                    <p className="text-text-muted mt-1">
                        {tenant.trial_active ? (
                            <>
                                You&apos;re on the {tenant.plan_tier} trial
                                {tenant.trial_ends_at
                                    ? ` until ${new Date(
                                          tenant.trial_ends_at,
                                      ).toLocaleDateString()}`
                                    : ""}{" "}
                                — Linda is listening.
                            </>
                        ) : tenant.trial_expired ? (
                            <>
                                Your trial has ended — upgrade to keep analyzing
                                calls.
                            </>
                        ) : (
                            <>Here&apos;s your week at a glance.</>
                        )}
                    </p>
                </div>
                {(notifs.data?.unread_count ?? 0) > 0 ? (
                    <Link
                        href="/notifications"
                        className="inline-flex items-center gap-2 rounded-full border border-border bg-bg-card px-3 py-1.5 text-xs hover:bg-bg-card-hover"
                    >
                        <span className="inline-block h-1.5 w-1.5 rounded-full bg-accent-rose" />
                        {notifs.data?.unread_count} new notification
                        {notifs.data?.unread_count === 1 ? "" : "s"}
                    </Link>
                ) : null}
            </header>

            {/* Quick actions — persistent (no longer empty-state-only) */}
            <QuickActionStrip />

            {/* Period selector */}
            <div
                className="flex items-center gap-2"
                role="group"
                aria-label="Dashboard period"
            >
                <span className="text-xs uppercase tracking-wide text-text-subtle">
                    Period
                </span>
                <div className="inline-flex rounded-md border border-border">
                    {PERIODS.map((p) => (
                        <button
                            key={p.value}
                            type="button"
                            onClick={() => setPeriod(p.value)}
                            aria-pressed={period === p.value}
                            className={
                                "px-3 py-1 text-xs " +
                                (period === p.value
                                    ? "bg-primary text-white"
                                    : "text-text-muted hover:bg-bg-card-hover")
                            }
                        >
                            {p.label}
                        </button>
                    ))}
                </div>
            </div>

            {/* KPI cards — replaced AI Health with QA Score; added rapport, churn, upsell */}
            <section className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-6">
                <StatCard
                    label="Calls"
                    loading={summary.isLoading}
                    value={
                        summary.data
                            ? String(summary.data.total_interactions)
                            : "—"
                    }
                    delta={summary.data?.prev_period_deltas?.total_interactions_pct}
                />
                <StatCard
                    label="Action items"
                    loading={summary.isLoading}
                    value={
                        summary.data
                            ? String(summary.data.action_items_open)
                            : "—"
                    }
                    hint={
                        summary.data && summary.data.overdue_action_items > 0
                            ? `${summary.data.overdue_action_items} overdue`
                            : undefined
                    }
                    hintTone={
                        summary.data && summary.data.overdue_action_items > 0
                            ? "rose"
                            : "subtle"
                    }
                />
                <StatCard
                    label="Avg sentiment"
                    loading={summary.isLoading}
                    value={
                        summary.data?.avg_sentiment_score != null
                            ? summary.data.avg_sentiment_score.toFixed(1)
                            : "—"
                    }
                    suffix={
                        summary.data?.avg_sentiment_score != null
                            ? "/ 10"
                            : undefined
                    }
                    delta={summary.data?.prev_period_deltas?.avg_sentiment_pct}
                />
                <StatCard
                    label="QA score"
                    loading={summary.isLoading}
                    value={
                        summary.data?.avg_qa_score != null
                            ? summary.data.avg_qa_score.toFixed(0)
                            : "—"
                    }
                    suffix={
                        summary.data?.avg_qa_score != null
                            ? "/ 100"
                            : undefined
                    }
                    delta={summary.data?.prev_period_deltas?.avg_qa_pct}
                />
                <StatCard
                    label="Rapport (LSM)"
                    loading={summary.isLoading}
                    value={
                        summary.data?.avg_rapport != null
                            ? Math.round(summary.data.avg_rapport * 100).toString()
                            : "—"
                    }
                    suffix={
                        summary.data?.avg_rapport != null ? "/ 100" : undefined
                    }
                />
                <StatCard
                    label="Talk %"
                    loading={manager.isLoading}
                    value={
                        manager.data?.talk_listen.tenant_avg_talk_pct != null
                            ? Math.round(
                                  manager.data.talk_listen.tenant_avg_talk_pct *
                                      100,
                              ).toString()
                            : "—"
                    }
                    suffix={
                        manager.data?.talk_listen.tenant_avg_talk_pct != null
                            ? "%"
                            : undefined
                    }
                    hint={!isManager ? "Manager view" : undefined}
                />
            </section>

            {/* Risk & opportunity row */}
            <section className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                <SignalCard
                    title="Churn risk"
                    tone="rose"
                    count={summary.data?.at_risk_count ?? 0}
                    avg={signals.data?.avg_churn_risk ?? null}
                    loading={summary.isLoading}
                    href="/customers?risk=churn"
                />
                <SignalCard
                    title="Upsell opportunity"
                    tone="emerald"
                    count={summary.data?.upsell_count ?? 0}
                    avg={signals.data?.avg_upsell_score ?? null}
                    loading={summary.isLoading}
                    href="/customers?signal=upsell"
                />
            </section>

            {/* Pipeline alerts — show only if there's something to alert on */}
            {summary.data &&
            (summary.data.flagged_for_review_count > 0 ||
                summary.data.failed_count > 0 ||
                summary.data.processing_count > 0 ||
                summary.data.overdue_action_items > 0) ? (
                <section
                    className="flex flex-wrap gap-2"
                    aria-label="Needs attention"
                >
                    {summary.data.overdue_action_items > 0 && (
                        <AlertChip
                            tone="rose"
                            href="/action-items?status=open"
                            label={`${summary.data.overdue_action_items} overdue action item${summary.data.overdue_action_items === 1 ? "" : "s"}`}
                        />
                    )}
                    {summary.data.flagged_for_review_count > 0 && (
                        <AlertChip
                            tone="amber"
                            href="/customers?tab=all-interactions&status=flagged_for_review"
                            label={`${summary.data.flagged_for_review_count} awaiting review`}
                        />
                    )}
                    {summary.data.failed_count > 0 && (
                        <AlertChip
                            tone="rose"
                            href="/customers?tab=all-interactions&status=failed"
                            label={`${summary.data.failed_count} failed`}
                        />
                    )}
                    {summary.data.processing_count > 0 && (
                        <AlertChip
                            tone="amber"
                            href="/customers?tab=all-interactions&status=processing"
                            label={`${summary.data.processing_count} processing`}
                        />
                    )}
                </section>
            ) : null}

            {/* Trends chart — calls + sentiment + QA + rapport */}
            <Panel title={`Trends · last ${period}`}>
                {trends.isLoading ? (
                    <div className="px-3 py-6 text-sm text-text-subtle">
                        Loading trends…
                    </div>
                ) : trends.error ? (
                    <ErrorRow message="Couldn't load trends." />
                ) : trendPoints.length > 0 ? (
                    <div className="px-3 py-3">
                        <TrendsChart
                            points={trendPoints}
                            qaByDate={qaByDate}
                            rapportByDate={rapportByDate}
                            height={220}
                        />
                    </div>
                ) : (
                    <EmptyPanel
                        title="No data for this period yet"
                        body="Once Linda analyzes a few calls, this chart fills in."
                    />
                )}
            </Panel>

            {/* Recent + action items (kept) */}
            <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
                <Panel
                    title="Recent calls"
                    action={
                        <Link
                            href="/customers?tab=all-interactions"
                            className="text-sm text-primary hover:underline"
                        >
                            View all →
                        </Link>
                    }
                >
                    {recent.isLoading ? (
                        <RowSkeleton rows={3} />
                    ) : recent.error ? (
                        <ErrorRow message="Couldn't load recent calls." />
                    ) : recent.data && recent.data.length > 0 ? (
                        <ul className="divide-y divide-border">
                            {recent.data.map((row) => (
                                <RecentCallRow key={row.id} row={row} />
                            ))}
                        </ul>
                    ) : (
                        <EmptyPanel
                            title="No calls yet"
                            body="Upload your first recording — Linda will transcribe and surface the moments that matter."
                            cta={{
                                href: "/interactions",
                                label: "Upload your first call",
                            }}
                        />
                    )}
                </Panel>

                <Panel
                    title="Open action items"
                    action={
                        <Link
                            href="/action-items"
                            className="text-sm text-primary hover:underline"
                        >
                            View all →
                        </Link>
                    }
                >
                    {actionItems.isLoading ? (
                        <RowSkeleton rows={3} />
                    ) : actionItems.error ? (
                        <ErrorRow message="Couldn't load action items." />
                    ) : actionItems.data && actionItems.data.length > 0 ? (
                        <ul className="divide-y divide-border">
                            {actionItems.data.map((item) => (
                                <ActionItemRow key={item.id} item={item} />
                            ))}
                        </ul>
                    ) : (
                        <EmptyPanel
                            title="Nothing to do — yet"
                            body="Linda flags follow-ups from each call here once one's processed."
                        />
                    )}
                </Panel>
            </div>

            {/* Channel breakdown + Top topics */}
            <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
                <Panel title="By channel">
                    {business.isLoading ? (
                        <RowSkeleton rows={3} />
                    ) : business.error ? (
                        <ErrorRow message="Couldn't load channel breakdown." />
                    ) : business.data &&
                      business.data.channels_breakdown.length > 0 ? (
                        <ChannelBars
                            rows={business.data.channels_breakdown}
                            total={business.data.total_interactions}
                        />
                    ) : (
                        <EmptyPanel
                            title="No channel data yet"
                            body="Once you ingest from voice, email, or chat, the mix shows up here."
                        />
                    )}
                </Panel>

                <Panel title="Top topics">
                    {business.isLoading ? (
                        <RowSkeleton rows={3} />
                    ) : business.error ? (
                        <ErrorRow message="Couldn't load topics." />
                    ) : business.data && business.data.top_topics.length > 0 ? (
                        <ul className="divide-y divide-border">
                            {business.data.top_topics.slice(0, 6).map((t) => (
                                <li
                                    key={t.name}
                                    className="flex items-center justify-between gap-3 px-3 py-2 text-sm"
                                >
                                    <span className="truncate font-medium">
                                        {t.name}
                                    </span>
                                    <span className="shrink-0 text-xs text-text-subtle">
                                        {t.mentions} mention
                                        {t.mentions === 1 ? "" : "s"}
                                    </span>
                                </li>
                            ))}
                        </ul>
                    ) : (
                        <EmptyPanel
                            title="No topic data yet"
                            body="Linda extracts topics from each call as it analyzes them."
                        />
                    )}
                </Panel>
            </div>

            {/* Coaching focus + Top blockers (gaps) */}
            <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
                <Panel title="Coaching focus">
                    {coaching.isLoading ? (
                        <RowSkeleton rows={3} />
                    ) : coaching.error ? (
                        <ErrorRow message="Couldn't load coaching insights." />
                    ) : coaching.data ? (
                        <CoachingFocus
                            adherence={coaching.data.avg_script_adherence}
                            improvements={coaching.data.top_improvements}
                            strengths={coaching.data.top_strengths}
                            methodology={
                                isManager ? manager.data?.methodology : undefined
                            }
                        />
                    ) : (
                        <EmptyPanel
                            title="Nothing to coach on yet"
                            body="Linda highlights coaching opportunities once you've ingested a few calls."
                        />
                    )}
                </Panel>

                <Panel title="Top blockers">
                    {coaching.isLoading ? (
                        <RowSkeleton rows={3} />
                    ) : coaching.error ? (
                        <ErrorRow message="Couldn't load blockers." />
                    ) : coaching.data &&
                      coaching.data.top_compliance_gaps.length > 0 ? (
                        <ul className="divide-y divide-border">
                            {coaching.data.top_compliance_gaps
                                .slice(0, 6)
                                .map((g) => (
                                    <li
                                        key={g.text}
                                        className="flex items-start justify-between gap-3 px-3 py-2 text-sm"
                                    >
                                        <span className="text-text-muted">
                                            {g.text}
                                        </span>
                                        <span className="shrink-0 text-xs text-text-subtle">
                                            ×{g.count}
                                        </span>
                                    </li>
                                ))}
                        </ul>
                    ) : (
                        <EmptyPanel
                            title="No blockers identified"
                            body="Linda surfaces objections and gaps here as patterns emerge."
                        />
                    )}
                </Panel>
            </div>

            {/* Account health */}
            <section className="grid grid-cols-1 gap-6 lg:grid-cols-3">
                <Panel title="At-risk accounts">
                    {accounts.isLoading ? (
                        <RowSkeleton rows={3} />
                    ) : accounts.error ? (
                        <ErrorRow message="Couldn't load at-risk accounts." />
                    ) : accounts.data && accounts.data.at_risk.length > 0 ? (
                        <AccountList
                            rows={accounts.data.at_risk}
                            scoreLabel="risk"
                            tone="rose"
                        />
                    ) : (
                        <EmptyPanel
                            title="No churn signals"
                            body="Customers showing churn risk will appear here."
                        />
                    )}
                </Panel>
                <Panel title="Expansion opportunities">
                    {accounts.isLoading ? (
                        <RowSkeleton rows={3} />
                    ) : accounts.error ? (
                        <ErrorRow message="Couldn't load opportunities." />
                    ) : accounts.data && accounts.data.upsell.length > 0 ? (
                        <AccountList
                            rows={accounts.data.upsell}
                            scoreLabel="upsell"
                            tone="emerald"
                        />
                    ) : (
                        <EmptyPanel
                            title="No upsell signals yet"
                            body="When Linda detects buying signals, they show up here."
                        />
                    )}
                </Panel>
                <Panel title="Stale accounts">
                    {accounts.isLoading ? (
                        <RowSkeleton rows={3} />
                    ) : accounts.error ? (
                        <ErrorRow message="Couldn't load stale accounts." />
                    ) : accounts.data && accounts.data.stale.length > 0 ? (
                        <AccountList
                            rows={accounts.data.stale}
                            scoreLabel="stale"
                            tone="amber"
                        />
                    ) : (
                        <EmptyPanel
                            title="Everyone's heard from you"
                            body="No accounts have gone quiet in the last 30 days."
                        />
                    )}
                </Panel>
            </section>

            {/* Integration health */}
            <Panel
                title="Integrations"
                action={
                    <Link
                        href="/settings"
                        className="text-sm text-primary hover:underline"
                    >
                        Manage →
                    </Link>
                }
            >
                <IntegrationStatus
                    loading={oauth.isLoading}
                    integrations={oauth.data?.integrations ?? []}
                />
            </Panel>

            {/* Quick start — only when truly empty */}
            {isEmpty ? (
                <section className="rounded-lg border border-border bg-bg-card p-6">
                    <h3 className="text-sm font-semibold text-text-muted">
                        Quick start
                    </h3>
                    <ol className="mt-4 grid grid-cols-1 gap-4 sm:grid-cols-3">
                        <QuickStep
                            n={1}
                            title="Upload a call"
                            body="Drop in a recording and Linda transcribes + analyzes it in minutes."
                            href="/customers?tab=all-interactions"
                            cta="Upload"
                        />
                        <QuickStep
                            n={2}
                            title="Connect a CRM"
                            body="Push outcomes and action items back to your system of record."
                            href="/settings"
                            cta="Configure"
                        />
                        <QuickStep
                            n={3}
                            title="Invite teammates"
                            body="Bring your team in to review calls and close the loop."
                            href="/settings"
                            cta="Invite"
                        />
                    </ol>
                </section>
            ) : null}
        </div>
    );
}

/* ── Subcomponents ──────────────────────────────────────────────────── */

function StatCard({
    label,
    value,
    loading,
    suffix,
    delta,
    hint,
    hintTone,
}: {
    label: string;
    value: string;
    loading?: boolean;
    suffix?: string;
    delta?: number | null;
    hint?: string;
    hintTone?: "rose" | "subtle";
}) {
    return (
        <div className="rounded-lg border border-border bg-bg-card p-4">
            <p className="text-xs uppercase tracking-wide text-text-subtle">
                {label}
            </p>
            <div className="mt-2 flex items-baseline gap-2">
                {loading ? (
                    <span
                        className="inline-block h-6 w-12 animate-pulse rounded bg-bg-card-hover"
                        aria-label="loading"
                    />
                ) : (
                    <>
                        <span className="text-2xl font-semibold">{value}</span>
                        {suffix ? (
                            <span className="text-sm text-text-subtle">
                                {suffix}
                            </span>
                        ) : null}
                    </>
                )}
            </div>
            {!loading && delta != null ? (
                <p
                    className={`mt-1 text-xs ${
                        delta > 0
                            ? "text-accent-emerald"
                            : delta < 0
                              ? "text-accent-rose"
                              : "text-text-subtle"
                    }`}
                >
                    {delta > 0 ? "▲" : delta < 0 ? "▼" : "•"} {Math.abs(delta)}%
                    vs prior period
                </p>
            ) : null}
            {hint && !loading ? (
                <p
                    className={`mt-1 text-xs ${
                        hintTone === "rose"
                            ? "text-accent-rose"
                            : "text-text-subtle"
                    }`}
                >
                    {hint}
                </p>
            ) : null}
        </div>
    );
}

function SignalCard({
    title,
    tone,
    count,
    avg,
    loading,
    href,
}: {
    title: string;
    tone: "rose" | "emerald";
    count: number;
    avg: number | null;
    loading?: boolean;
    href: string;
}) {
    const dot = tone === "rose" ? "bg-accent-rose" : "bg-accent-emerald";
    return (
        <Link
            href={href}
            className="group rounded-lg border border-border bg-bg-card p-4 hover:bg-bg-card-hover"
        >
            <div className="flex items-start justify-between">
                <div className="flex items-center gap-2">
                    <span className={`inline-block h-2 w-2 rounded-full ${dot}`} />
                    <p className="text-sm font-semibold">{title}</p>
                </div>
                <span className="text-xs text-text-subtle group-hover:text-text-muted">
                    View →
                </span>
            </div>
            <div className="mt-2 flex items-baseline gap-3">
                {loading ? (
                    <span className="inline-block h-7 w-12 animate-pulse rounded bg-bg-card-hover" />
                ) : (
                    <>
                        <span className="text-2xl font-semibold">{count}</span>
                        <span className="text-xs text-text-subtle">
                            calls flagged
                        </span>
                    </>
                )}
            </div>
            {avg != null ? (
                <p className="mt-1 text-xs text-text-subtle">
                    Avg score: {avg.toFixed(2)}
                </p>
            ) : null}
        </Link>
    );
}

function AlertChip({
    tone,
    href,
    label,
}: {
    tone: "rose" | "amber";
    href: string;
    label: string;
}) {
    const cls =
        tone === "rose"
            ? "border-accent-rose/40 text-accent-rose hover:bg-accent-rose/10"
            : "border-accent-amber/40 text-accent-amber hover:bg-accent-amber/10";
    return (
        <Link
            href={href}
            className={`inline-flex items-center gap-2 rounded-full border bg-bg-card px-3 py-1.5 text-xs ${cls}`}
        >
            {label} →
        </Link>
    );
}

function QuickActionStrip() {
    return (
        <section
            className="flex flex-wrap gap-2"
            aria-label="Quick actions"
        >
            <Link
                href="/customers?tab=all-interactions"
                className="inline-flex items-center gap-2 rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-white hover:bg-primary-hover"
            >
                + Upload a call
            </Link>
            <Link
                href="/settings"
                className="inline-flex items-center gap-2 rounded-md border border-border bg-bg-card px-3 py-1.5 text-sm hover:bg-bg-card-hover"
            >
                Connect CRM
            </Link>
            <Link
                href="/settings"
                className="inline-flex items-center gap-2 rounded-md border border-border bg-bg-card px-3 py-1.5 text-sm hover:bg-bg-card-hover"
            >
                Invite teammates
            </Link>
            <Link
                href="/notifications"
                className="inline-flex items-center gap-2 rounded-md border border-border bg-bg-card px-3 py-1.5 text-sm hover:bg-bg-card-hover"
            >
                Notifications
            </Link>
        </section>
    );
}

function ChannelBars({
    rows,
    total,
}: {
    rows: { channel: string; count: number; avg_sentiment: number | null }[];
    total: number;
}) {
    return (
        <ul className="space-y-2 px-3 py-3">
            {rows.map((r) => {
                const pct = total > 0 ? Math.round((r.count / total) * 100) : 0;
                const sent = sentimentLabel(r.avg_sentiment);
                return (
                    <li key={r.channel}>
                        <div className="flex items-center justify-between text-sm">
                            <span className="font-medium capitalize">
                                {r.channel}
                            </span>
                            <span className="text-xs text-text-subtle">
                                {r.count} · {pct}%{" "}
                                <span className={toneClass(sent.tone)}>
                                    · {sent.text}
                                </span>
                            </span>
                        </div>
                        <div className="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-bg-secondary">
                            <div
                                className="h-full bg-primary"
                                style={{ width: `${pct}%` }}
                            />
                        </div>
                    </li>
                );
            })}
        </ul>
    );
}

function CoachingFocus({
    adherence,
    improvements,
    strengths,
    methodology,
}: {
    adherence: number | null;
    improvements: Array<{ text: string; count: number }>;
    strengths: Array<{ text: string; count: number }>;
    methodology?: Array<{
        framework: string;
        avg_coverage_ratio: number | null;
        most_missed_stage: string | null;
    }>;
}) {
    const focus = improvements[0]?.text;
    const strength = strengths[0]?.text;
    const adherencePct = adherence != null ? Math.round(adherence * 100) : null;

    return (
        <div className="space-y-3 px-3 py-3">
            <div className="grid grid-cols-2 gap-3">
                <Mini label="Script adherence">
                    {adherencePct != null ? `${adherencePct}%` : "—"}
                </Mini>
                <Mini label="Methodology">
                    {methodology && methodology.length > 0
                        ? methodology
                              .map(
                                  (m) =>
                                      `${m.framework}: ${
                                          m.avg_coverage_ratio != null
                                              ? `${Math.round(m.avg_coverage_ratio * 100)}%`
                                              : "—"
                                      }`,
                              )
                              .join(" · ")
                        : methodology
                          ? "No data"
                          : "Manager view"}
                </Mini>
            </div>
            {focus ? (
                <div className="rounded-md border border-border bg-bg-secondary p-3">
                    <p className="text-xs uppercase tracking-wide text-text-subtle">
                        Focus this week
                    </p>
                    <p className="mt-1 text-sm">{focus}</p>
                </div>
            ) : null}
            {strength ? (
                <div className="rounded-md border border-border bg-bg-secondary p-3">
                    <p className="text-xs uppercase tracking-wide text-text-subtle">
                        Keep doing
                    </p>
                    <p className="mt-1 text-sm text-accent-emerald">{strength}</p>
                </div>
            ) : null}
            {methodology && methodology.length > 0
                ? methodology[0].most_missed_stage && (
                      <p className="text-xs text-text-subtle">
                          Most-missed stage:{" "}
                          <span className="text-text-muted">
                              {methodology[0].most_missed_stage}
                          </span>
                      </p>
                  )
                : null}
        </div>
    );
}

function Mini({
    label,
    children,
}: {
    label: string;
    children: React.ReactNode;
}) {
    return (
        <div className="rounded-md border border-border bg-bg-secondary p-3">
            <p className="text-xs uppercase tracking-wide text-text-subtle">
                {label}
            </p>
            <p className="mt-1 text-sm font-semibold">{children}</p>
        </div>
    );
}

function AccountList({
    rows,
    scoreLabel,
    tone,
}: {
    rows: AccountHealthRow[];
    scoreLabel: "risk" | "upsell" | "stale";
    tone: "rose" | "emerald" | "amber";
}) {
    return (
        <ul className="divide-y divide-border">
            {rows.map((r) => (
                <li key={r.customer_id}>
                    <Link
                        href={`/customers/${r.customer_id}`}
                        className="flex items-center justify-between gap-3 rounded-md px-3 py-2 text-sm hover:bg-bg-card-hover"
                    >
                        <span className="min-w-0 flex-1 truncate font-medium">
                            {r.name}
                        </span>
                        <span
                            className={`shrink-0 text-xs ${toneClass(tone)}`}
                        >
                            {scoreLabel === "stale"
                                ? r.last_touch_at
                                    ? `last ${formatRelative(r.last_touch_at)}`
                                    : "no contact"
                                : r.score != null
                                  ? r.score.toFixed(2)
                                  : "—"}
                        </span>
                    </Link>
                </li>
            ))}
        </ul>
    );
}

function IntegrationStatus({
    loading,
    integrations,
}: {
    loading: boolean;
    integrations: { id: string; provider: string; expires_at: string | null }[];
}) {
    if (loading) {
        return <RowSkeleton rows={2} />;
    }
    // Show a fixed lineup so missing integrations read as "not connected"
    // rather than just being absent.
    const expected = [
        { key: "salesforce", label: "Salesforce" },
        { key: "hubspot", label: "HubSpot" },
        { key: "pipedrive", label: "Pipedrive" },
        { key: "google", label: "Google" },
        { key: "microsoft", label: "Microsoft" },
    ];
    const byProvider = new Map(integrations.map((i) => [i.provider, i]));
    return (
        <ul className="grid grid-cols-2 gap-2 px-3 py-3 sm:grid-cols-3 lg:grid-cols-5">
            {expected.map((e) => {
                const conn = byProvider.get(e.key);
                const expired =
                    conn?.expires_at != null &&
                    new Date(conn.expires_at).getTime() < Date.now();
                const tone = !conn
                    ? "subtle"
                    : expired
                      ? "amber"
                      : "emerald";
                return (
                    <li
                        key={e.key}
                        className="flex items-center justify-between rounded-md border border-border bg-bg-secondary px-3 py-2 text-xs"
                    >
                        <span className="font-medium">{e.label}</span>
                        <span className={toneClass(tone)}>
                            {!conn
                                ? "Not connected"
                                : expired
                                  ? "Token expired"
                                  : "Connected"}
                        </span>
                    </li>
                );
            })}
        </ul>
    );
}

function Panel({
    title,
    action,
    children,
}: {
    title: string;
    action?: React.ReactNode;
    children: React.ReactNode;
}) {
    return (
        <section className="rounded-lg border border-border bg-bg-card">
            <div className="flex items-center justify-between border-b border-border px-5 py-3">
                <h3 className="text-sm font-semibold">{title}</h3>
                {action}
            </div>
            <div className="px-2 py-1">{children}</div>
        </section>
    );
}

function RecentCallRow({ row }: { row: InteractionOut }) {
    const sent = sentimentLabel(row.insights?.sentiment_score);
    return (
        <li>
            <Link
                href={`/interactions/${row.id}`}
                className="flex items-center justify-between gap-3 rounded-md px-3 py-3 hover:bg-bg-card-hover"
            >
                <div className="min-w-0 flex-1">
                    <div className="truncate text-sm font-medium">
                        {row.title || row.caller_phone || "Untitled call"}
                    </div>
                    <div className="mt-0.5 text-xs text-text-subtle">
                        {formatRelative(row.created_at)} · {row.channel}
                    </div>
                </div>
                <span
                    className={`shrink-0 text-xs ${toneClass(sent.tone)}`}
                    title="Sentiment"
                >
                    {sent.text}
                </span>
                <StatusPill status={row.status} />
            </Link>
        </li>
    );
}

function ActionItemRow({ item }: { item: ActionItemOut }) {
    return (
        <li>
            <Link
                href={`/interactions/${item.interaction_id}`}
                className="flex items-center justify-between gap-3 rounded-md px-3 py-3 hover:bg-bg-card-hover"
            >
                <div className="min-w-0 flex-1">
                    <div className="truncate text-sm font-medium">
                        {item.title}
                    </div>
                    <div className="mt-0.5 text-xs text-text-subtle">
                        {item.priority} · due{" "}
                        {item.due_date
                            ? new Date(item.due_date).toLocaleDateString()
                            : "—"}
                    </div>
                </div>
                <span className="text-xs uppercase tracking-wide text-text-subtle">
                    {item.status}
                </span>
            </Link>
        </li>
    );
}

function StatusPill({ status }: { status: string }) {
    const tone =
        status === "analyzed"
            ? "emerald"
            : status === "failed"
              ? "rose"
              : status === "processing"
                ? "amber"
                : "subtle";
    return (
        <span
            className={`shrink-0 rounded-full border border-border px-2 py-0.5 text-xs ${toneClass(tone)}`}
        >
            {status}
        </span>
    );
}

function toneClass(tone: "emerald" | "amber" | "rose" | "subtle"): string {
    switch (tone) {
        case "emerald":
            return "text-accent-emerald";
        case "amber":
            return "text-accent-amber";
        case "rose":
            return "text-accent-rose";
        default:
            return "text-text-subtle";
    }
}

function EmptyPanel({
    title,
    body,
    cta,
}: {
    title: string;
    body: string;
    cta?: { href: string; label: string };
}) {
    return (
        <div className="px-4 py-8 text-center">
            <p className="text-sm font-medium">{title}</p>
            <p className="mx-auto mt-1 max-w-sm text-sm text-text-muted">
                {body}
            </p>
            {cta ? (
                <Link
                    href={cta.href}
                    className="mt-4 inline-flex rounded-md bg-primary px-3 py-2 text-sm font-medium text-white hover:bg-primary-hover"
                >
                    {cta.label}
                </Link>
            ) : null}
        </div>
    );
}

function RowSkeleton({ rows }: { rows: number }) {
    return (
        <ul className="divide-y divide-border">
            {Array.from({ length: rows }).map((_, i) => (
                <li
                    key={i}
                    className="flex items-center justify-between gap-3 px-3 py-3"
                >
                    <div className="flex-1 animate-pulse space-y-2">
                        <div className="h-3 w-2/3 rounded bg-bg-card-hover" />
                        <div className="h-2 w-1/3 rounded bg-bg-card-hover" />
                    </div>
                </li>
            ))}
        </ul>
    );
}

function ErrorRow({ message }: { message: string }) {
    return (
        <p className="px-4 py-6 text-center text-sm text-accent-rose">
            {message}
        </p>
    );
}

function QuickStep({
    n,
    title,
    body,
    href,
    cta,
}: {
    n: number;
    title: string;
    body: string;
    href: string;
    cta: string;
}) {
    return (
        <li className="rounded-md border border-border bg-bg-secondary p-4">
            <div className="flex items-center gap-2">
                <span className="flex h-6 w-6 items-center justify-center rounded-full bg-primary text-xs font-semibold text-white">
                    {n}
                </span>
                <span className="text-sm font-semibold">{title}</span>
            </div>
            <p className="mt-2 text-sm text-text-muted">{body}</p>
            <Link
                href={href}
                className="mt-3 inline-flex text-sm text-primary hover:underline"
            >
                {cta} →
            </Link>
        </li>
    );
}

/* ── helpers ─────────────────────────────────────────────────────── */

import type { TrendPoint } from "@/lib/analytics";
import type { DailyPoint } from "@/components/dashboard/trends-chart";

function useMemoTrendPoints(raw: TrendPoint[] | undefined): DailyPoint[] {
    return useMemo(() => {
        if (!raw || raw.length === 0) return [];
        const byDate = new Map<string, DailyPoint & { _w: number }>();
        for (const r of raw) {
            // channel=null rows carry QA + rapport supplements only —
            // include the date so the x-axis stays continuous, but
            // don't double-count interactions.
            if (r.channel == null) {
                if (!byDate.has(r.date)) {
                    byDate.set(r.date, {
                        date: r.date,
                        interaction_count: 0,
                        avg_sentiment: null,
                        _w: 0,
                    });
                }
                continue;
            }
            const cur = byDate.get(r.date) ?? {
                date: r.date,
                interaction_count: 0,
                avg_sentiment: null,
                _w: 0,
            };
            cur.interaction_count += r.interaction_count;
            if (r.avg_sentiment != null) {
                const prevW = cur._w;
                const prevSum = (cur.avg_sentiment ?? 0) * prevW;
                const w = r.interaction_count;
                cur._w = prevW + w;
                cur.avg_sentiment =
                    cur._w > 0 ? (prevSum + r.avg_sentiment * w) / cur._w : null;
            }
            byDate.set(r.date, cur);
        }
        return Array.from(byDate.values())
            .sort((a, b) => a.date.localeCompare(b.date))
            .map(({ _w, ...rest }) => {
                void _w;
                return rest;
            });
    }, [raw]);
}
