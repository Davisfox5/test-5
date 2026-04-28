"use client";

import Link from "next/link";
import { useMe } from "@/lib/me";
import {
    formatRelative,
    sentimentLabel,
    useAiHealth,
    useDashboardSummary,
    useInteractions,
    useOpenActionItems,
    type ActionItemOut,
    type InteractionOut,
} from "@/lib/interactions";

export default function DashboardPage() {
    const me = useMe();
    const summary = useDashboardSummary();
    const aiHealth = useAiHealth();
    const recent = useInteractions({ limit: 5 });
    const actionItems = useOpenActionItems(5);

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
            <header>
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
                        <>Your trial has ended — upgrade to keep analyzing calls.</>
                    ) : (
                        <>Here&apos;s your week at a glance.</>
                    )}
                </p>
            </header>

            <section className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
                <StatCard
                    label="Calls this period"
                    loading={summary.isLoading}
                    value={
                        summary.data
                            ? String(summary.data.total_interactions)
                            : "—"
                    }
                    delta={summary.data?.prev_period_deltas?.total_interactions_pct}
                />
                <StatCard
                    label="Action items open"
                    loading={summary.isLoading}
                    value={
                        summary.data
                            ? String(summary.data.action_items_open)
                            : "—"
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
                    label="AI health"
                    loading={aiHealth.isLoading}
                    // Force the value to "—" on error so a 500 from the
                    // analytics endpoint can never leave the card stuck
                    // on a stale number from a prior render.
                    value={
                        aiHealth.error
                            ? "—"
                            : aiHealth.data?.quality_score_avg_7d != null
                              ? aiHealth.data.quality_score_avg_7d.toFixed(1)
                              : "—"
                    }
                    hint={
                        aiHealth.error
                            ? "No data yet"
                            : undefined
                    }
                />
            </section>

            <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
                <Panel
                    title="Recent calls"
                    action={
                        <Link
                            href="/interactions"
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
                            body="Linda will surface action items here as you process calls."
                        />
                    )}
                </Panel>
            </div>

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
                            href="/interactions"
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
}: {
    label: string;
    value: string;
    loading?: boolean;
    suffix?: string;
    delta?: number | null;
    hint?: string;
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
                <p className="mt-1 text-xs text-text-subtle">{hint}</p>
            ) : null}
        </div>
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
