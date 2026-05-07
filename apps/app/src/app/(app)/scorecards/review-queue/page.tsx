"use client";

import Link from "next/link";
import { useState } from "react";

import {
    type ReviewQueueItem,
    type TriagePriority,
    useResolveReviewItem,
    useReviewQueue,
} from "@/lib/scorecards";
import { formatDuration, formatRelative } from "@/lib/interactions";

/**
 * Scorecard review queue.
 *
 * Manager + admin surface that lists every interaction the LLM judge
 * marked ``flagged_for_review`` (composite < 0.5 or any dimension <
 * 0.4) plus a research-derived triage band so the manager works the
 * highest-stakes calls first. The "resolve" action flips the status
 * back to ``analyzed`` — the underlying quality scores stay attached
 * for trend reporting.
 */

const TRIAGE_TABS: Array<{ key: TriagePriority | "all"; label: string }> = [
    { key: "all", label: "All" },
    { key: "high", label: "High priority" },
    { key: "medium", label: "Medium" },
    { key: "low", label: "Low" },
];

const TRIAGE_TONE: Record<TriagePriority, string> = {
    high: "border-accent-rose/40 bg-accent-rose/10 text-accent-rose",
    medium: "border-accent-amber/40 bg-accent-amber/10 text-accent-amber",
    low: "border-border bg-bg-secondary text-text-muted",
};

export default function ScorecardReviewQueuePage() {
    const [filter, setFilter] = useState<TriagePriority | "all">("all");
    const queue = useReviewQueue(filter === "all" ? undefined : filter);
    const resolve = useResolveReviewItem();

    return (
        <div className="space-y-6">
            <header className="flex flex-wrap items-start justify-between gap-4">
                <div>
                    <h2 className="text-2xl font-bold">Review queue</h2>
                    <p className="mt-1 text-sm text-text-muted">
                        Calls Linda flagged for human review — composite
                        quality below threshold or a single dimension critically
                        low. Triage banding ranks fire-drill items above
                        training-data items.
                    </p>
                </div>
                <Link
                    href="/scorecards"
                    className="rounded-md border border-border bg-bg-secondary px-3 py-1.5 text-sm text-text-muted hover:bg-card-hover"
                >
                    ← Back to scorecards
                </Link>
            </header>

            <div
                role="tablist"
                aria-label="Triage filter"
                className="flex flex-wrap gap-1 border-b border-border"
            >
                {TRIAGE_TABS.map((t) => (
                    <button
                        key={t.key}
                        type="button"
                        role="tab"
                        aria-selected={filter === t.key}
                        onClick={() => setFilter(t.key)}
                        className={`-mb-px rounded-t-md border-b-2 px-3 py-2 text-sm font-medium transition-colors ${
                            filter === t.key
                                ? "border-primary text-text"
                                : "border-transparent text-text-muted hover:text-text"
                        }`}
                    >
                        {t.label}
                    </button>
                ))}
            </div>

            {queue.isLoading ? (
                <p className="text-sm text-text-muted">Loading queue…</p>
            ) : queue.error ? (
                <p className="text-sm text-accent-rose">
                    Couldn&apos;t load the review queue.
                </p>
            ) : !queue.data || queue.data.length === 0 ? (
                <div className="rounded-lg border border-border bg-bg-card p-6 text-center text-sm text-text-muted">
                    Nothing in this band right now. The queue refreshes as
                    Linda&apos;s judge flags new interactions.
                </div>
            ) : (
                <ul className="space-y-3">
                    {queue.data.map((item) => (
                        <ReviewQueueRow
                            key={item.interaction_id}
                            item={item}
                            onResolve={() =>
                                resolve.mutate(item.interaction_id)
                            }
                            resolving={
                                resolve.isPending &&
                                resolve.variables === item.interaction_id
                            }
                        />
                    ))}
                </ul>
            )}
        </div>
    );
}

function ReviewQueueRow({
    item,
    onResolve,
    resolving,
}: {
    item: ReviewQueueItem;
    onResolve: () => void;
    resolving: boolean;
}) {
    const tone = TRIAGE_TONE[item.triage_priority];
    return (
        <li className="rounded-lg border border-border bg-bg-card p-4">
            <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                        <span
                            className={`rounded-full border px-2 py-0.5 text-xs font-medium uppercase tracking-wide ${tone}`}
                        >
                            {item.triage_priority}
                        </span>
                        <Link
                            href={`/interactions/${item.interaction_id}`}
                            className="truncate text-sm font-semibold text-text hover:underline"
                        >
                            {item.title || "(untitled call)"}
                        </Link>
                    </div>
                    <div className="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-xs text-text-muted">
                        <span>{formatRelative(item.created_at)}</span>
                        <span>•</span>
                        <span>{formatDuration(item.duration_seconds)}</span>
                        <span>•</span>
                        <span className="capitalize">{item.channel}</span>
                        {item.sentiment_overall ? (
                            <>
                                <span>•</span>
                                <span className="capitalize">
                                    sentiment: {item.sentiment_overall}
                                </span>
                            </>
                        ) : null}
                        {item.churn_risk_signal ? (
                            <>
                                <span>•</span>
                                <span className="capitalize">
                                    churn: {item.churn_risk_signal}
                                </span>
                            </>
                        ) : null}
                    </div>
                </div>
                <div className="flex shrink-0 items-center gap-2">
                    <Link
                        href={`/interactions/${item.interaction_id}#coaching`}
                        className="rounded border border-border bg-bg-secondary px-3 py-1.5 text-xs text-text hover:bg-card-hover"
                    >
                        Coach this call
                    </Link>
                    <button
                        type="button"
                        onClick={onResolve}
                        disabled={resolving}
                        className="rounded bg-primary px-3 py-1.5 text-xs font-medium text-white hover:bg-primary-hover disabled:opacity-60"
                    >
                        {resolving ? "Resolving…" : "Mark reviewed"}
                    </button>
                </div>
            </div>
            <div className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-3">
                <ScoreCell label="Composite" value={item.composite} />
                <ScoreCell
                    label={
                        item.weakest_dimension
                            ? `Weakest · ${item.weakest_dimension}`
                            : "Weakest dimension"
                    }
                    value={item.weakest_score}
                />
                <ScoreCell
                    label="Status"
                    text={item.status.replace(/_/g, " ")}
                />
            </div>
        </li>
    );
}

function ScoreCell({
    label,
    value,
    text,
}: {
    label: string;
    value?: number | null;
    text?: string;
}) {
    const display =
        text != null
            ? text
            : typeof value === "number"
              ? `${Math.round(value * 100)}%`
              : "—";
    return (
        <div className="rounded-md border border-border bg-bg-secondary px-3 py-2">
            <div className="text-xs uppercase tracking-wide text-text-subtle">
                {label}
            </div>
            <div className="mt-1 text-sm font-semibold capitalize text-text">
                {display}
            </div>
        </div>
    );
}
