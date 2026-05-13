"use client";

/**
 * Interactions — the global call feed.
 *
 * Two tabs:
 *   - All: paginated table of every analyzed call in the tenant.
 *     Filters: free-text search, status, date range. Rows drill into
 *     ``/interactions/{id}``.
 *   - Needs Review: same table filtered to non-terminal states
 *     (flagged_for_review, failed, transcription_failed, processing,
 *     transcription_pending). Dashboard alert chips deep-link here
 *     with ``?tab=needs-review&status=…``.
 *
 * Page replaced the prior simple "limit=200, sort client-side" list
 * with a paginated table (PR #104) while preserving the Needs Review
 * tab + URL-driven tab/status state introduced in commit 868a266.
 */

import Link from "next/link";
import { useMemo, useState } from "react";
import { useSearchParams, useRouter, usePathname } from "next/navigation";
import {
    formatDuration,
    formatRelative,
    sentimentLabel,
    useInteractions,
    type InteractionOut,
} from "@/lib/interactions";

const PAGE_SIZE = 25;
type Tab = "all" | "needs-review";

const NEEDS_REVIEW_STATUSES = new Set([
    "flagged_for_review",
    "failed",
    "transcription_failed",
    "processing",
    "transcription_pending",
]);

function readTab(value: string | null): Tab {
    return value === "needs-review" ? "needs-review" : "all";
}

export default function InteractionsPage() {
    const searchParams = useSearchParams();
    const router = useRouter();
    const pathname = usePathname();
    const tab = readTab(searchParams?.get("tab") ?? null);
    const setTab = (next: Tab) => {
        const params = new URLSearchParams(searchParams?.toString() ?? "");
        if (next === "all") {
            params.delete("tab");
            params.delete("status");
        } else {
            params.set("tab", next);
        }
        const qs = params.toString();
        router.replace(qs ? `${pathname}?${qs}` : pathname);
        setPage(0);
    };

    const [page, setPage] = useState(0);
    const [q, setQ] = useState("");
    // Status filter is seeded from ?status=… when the dashboard chip
    // deep-links into Needs Review; the user can still narrow further
    // inside the tab via the select.
    const [status, setStatus] = useState(
        tab === "needs-review" ? searchParams?.get("status") ?? "" : "",
    );
    const [from, setFrom] = useState("");
    const [to, setTo] = useState("");

    const list = useInteractions({
        limit: PAGE_SIZE + 1,
        offset: page * PAGE_SIZE,
        q: q || undefined,
        status: status || undefined,
    });

    const rawRows = list.data ?? [];
    const hasMore = rawRows.length > PAGE_SIZE;
    const visibleRows = hasMore ? rawRows.slice(0, PAGE_SIZE) : rawRows;

    const filteredRows = useMemo(() => {
        let rows = visibleRows;
        // Needs-Review tab narrows to non-terminal statuses regardless
        // of what the status select says. The select still works inside
        // that universe so the user can flip between "all flagged" and
        // "just processing", for example.
        if (tab === "needs-review") {
            rows = rows.filter((r) => NEEDS_REVIEW_STATUSES.has(r.status));
        }
        if (!from && !to) return rows;
        const fromTs = from ? new Date(from).getTime() : -Infinity;
        const toTs = to ? new Date(to).getTime() + 86_400_000 : Infinity;
        return rows.filter((r) => {
            const t = new Date(r.created_at).getTime();
            return t >= fromTs && t < toTs;
        });
    }, [visibleRows, from, to, tab]);

    const isFiltered = !!(q || status || from || to);
    const isEmpty =
        !list.isLoading &&
        !list.error &&
        filteredRows.length === 0 &&
        !isFiltered &&
        tab === "all";

    return (
        <div className="space-y-6">
            <header>
                <h1 className="text-2xl font-bold">Interactions</h1>
                <p className="text-sm text-text-muted">
                    Every analyzed call across the tenant. Filter, sort, and
                    drill into any row for the full detail layout.
                </p>
            </header>

            <div
                role="tablist"
                aria-label="Interaction sections"
                className="flex gap-2 border-b border-border"
            >
                {(["all", "needs-review"] as Tab[]).map((t) => {
                    const active = tab === t;
                    return (
                        <button
                            key={t}
                            type="button"
                            role="tab"
                            aria-selected={active}
                            onClick={() => setTab(t)}
                            className={`-mb-px border-b-2 px-4 py-2 text-sm transition-colors ${
                                active
                                    ? "border-primary text-primary"
                                    : "border-transparent text-text-muted hover:text-text"
                            }`}
                        >
                            {t === "all" ? "All" : "Needs Review"}
                        </button>
                    );
                })}
            </div>

            {tab === "needs-review" ? (
                <p className="text-sm text-text-muted">
                    Calls Linda couldn&apos;t complete or wants you to glance
                    at: flagged for review, processing, or failed. Open one
                    to see the reason and retry.
                </p>
            ) : null}

            <section className="rounded-lg border border-border bg-bg-card p-4">
                <div className="grid grid-cols-1 gap-3 sm:grid-cols-4">
                    <label className="block sm:col-span-2">
                        <span className="mb-1 block text-xs uppercase tracking-wide text-text-subtle">
                            Search
                        </span>
                        <input
                            type="search"
                            value={q}
                            onChange={(e) => {
                                setQ(e.target.value);
                                setPage(0);
                            }}
                            placeholder="Title or caller…"
                            className="w-full rounded-md border border-border bg-bg-secondary px-3 py-2 text-sm outline-none focus:border-primary"
                        />
                    </label>
                    <label className="block">
                        <span className="mb-1 block text-xs uppercase tracking-wide text-text-subtle">
                            Status
                        </span>
                        <select
                            value={status}
                            onChange={(e) => {
                                setStatus(e.target.value);
                                setPage(0);
                            }}
                            className="w-full rounded-md border border-border bg-bg-secondary px-3 py-2 text-sm outline-none focus:border-primary"
                        >
                            {tab === "needs-review" ? (
                                <>
                                    <option value="">
                                        All needs-review
                                    </option>
                                    <option value="flagged_for_review">
                                        Flagged for review
                                    </option>
                                    <option value="failed">Failed</option>
                                    <option value="processing">
                                        Processing
                                    </option>
                                </>
                            ) : (
                                <>
                                    <option value="">All</option>
                                    <option value="processing">
                                        Processing
                                    </option>
                                    <option value="analyzed">Analyzed</option>
                                    <option value="failed">Failed</option>
                                    <option value="flagged_for_review">
                                        Flagged for review
                                    </option>
                                </>
                            )}
                        </select>
                    </label>
                    <div className="grid grid-cols-2 gap-2">
                        <label className="block">
                            <span className="mb-1 block text-xs uppercase tracking-wide text-text-subtle">
                                From
                            </span>
                            <input
                                type="date"
                                value={from}
                                onChange={(e) => setFrom(e.target.value)}
                                className="w-full rounded-md border border-border bg-bg-secondary px-2 py-2 text-sm outline-none focus:border-primary"
                            />
                        </label>
                        <label className="block">
                            <span className="mb-1 block text-xs uppercase tracking-wide text-text-subtle">
                                To
                            </span>
                            <input
                                type="date"
                                value={to}
                                onChange={(e) => setTo(e.target.value)}
                                className="w-full rounded-md border border-border bg-bg-secondary px-2 py-2 text-sm outline-none focus:border-primary"
                            />
                        </label>
                    </div>
                </div>
            </section>

            <section className="overflow-hidden rounded-lg border border-border bg-bg-card">
                {list.isLoading ? (
                    <TableSkeleton />
                ) : list.error ? (
                    <p className="px-6 py-8 text-center text-sm text-accent-rose">
                        Couldn&apos;t load interactions.
                    </p>
                ) : isEmpty ? (
                    <EmptyState />
                ) : filteredRows.length === 0 ? (
                    <p className="px-6 py-8 text-center text-sm text-text-muted">
                        {tab === "needs-review"
                            ? "Nothing needs your review right now."
                            : "No interactions match your filters."}
                    </p>
                ) : (
                    <Table rows={filteredRows} />
                )}
            </section>

            <Pagination
                page={page}
                hasMore={hasMore}
                onPrev={() => setPage((p) => Math.max(0, p - 1))}
                onNext={() => setPage((p) => p + 1)}
            />
        </div>
    );
}

function Table({ rows }: { rows: InteractionOut[] }) {
    return (
        <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
                <thead className="border-b border-border bg-bg-secondary">
                    <tr className="text-xs uppercase tracking-wide text-text-subtle">
                        <Th>Date</Th>
                        <Th>Title / Caller</Th>
                        <Th>Duration</Th>
                        <Th>Sentiment</Th>
                        <Th>Status</Th>
                    </tr>
                </thead>
                <tbody>
                    {rows.map((row) => (
                        <Row key={row.id} row={row} />
                    ))}
                </tbody>
            </table>
        </div>
    );
}

function Row({ row }: { row: InteractionOut }) {
    const sent = sentimentLabel(row.insights?.sentiment_score);
    // When the resolver linked the call to a customer, land inside the
    // customer profile (Interactions tab, scrolled to the matching row);
    // otherwise the standalone call detail page.
    const href = row.customer_id
        ? `/customers/${row.customer_id}?tab=interactions&focus=interaction-${row.id}`
        : `/interactions/${row.id}`;
    return (
        <tr
            className="cursor-pointer border-b border-border last:border-b-0 hover:bg-bg-card-hover"
            onClick={() => {
                window.location.href = href;
            }}
        >
            <Td>
                <Link
                    href={href}
                    className="text-text hover:underline"
                    onClick={(e) => e.stopPropagation()}
                >
                    {formatRelative(row.created_at)}
                </Link>
            </Td>
            <Td>
                <span className="font-medium">
                    {row.title || row.caller_phone || "Untitled"}
                </span>
                <div className="text-xs text-text-subtle">{row.channel}</div>
            </Td>
            <Td>{formatDuration(row.duration_seconds)}</Td>
            <Td>
                <span
                    className={
                        sent.tone === "emerald"
                            ? "text-accent-emerald"
                            : sent.tone === "amber"
                              ? "text-accent-amber"
                              : sent.tone === "rose"
                                ? "text-accent-rose"
                                : "text-text-subtle"
                    }
                >
                    {sent.text}
                </span>
            </Td>
            <Td>
                <span className="rounded-full border border-border px-2 py-0.5 text-xs capitalize text-text-muted">
                    {row.status.replace(/_/g, " ")}
                </span>
            </Td>
        </tr>
    );
}

function Th({ children }: { children: React.ReactNode }) {
    return <th className="px-4 py-3 font-medium">{children}</th>;
}

function Td({ children }: { children: React.ReactNode }) {
    return <td className="px-4 py-3">{children}</td>;
}

function TableSkeleton() {
    return (
        <div className="divide-y divide-border">
            {Array.from({ length: 6 }).map((_, i) => (
                <div
                    key={i}
                    className="flex items-center gap-4 px-4 py-3 animate-pulse"
                >
                    <div className="h-3 w-20 rounded bg-bg-card-hover" />
                    <div className="h-3 flex-1 rounded bg-bg-card-hover" />
                    <div className="h-3 w-12 rounded bg-bg-card-hover" />
                    <div className="h-3 w-16 rounded bg-bg-card-hover" />
                </div>
            ))}
        </div>
    );
}

function EmptyState() {
    return (
        <div className="px-6 py-16 text-center">
            <p className="text-base font-semibold">No calls yet</p>
            <p className="mx-auto mt-2 max-w-md text-sm text-text-muted">
                Upload a recording or paste a URL — Linda transcribes and
                analyzes it, then you&apos;ll see it here.
            </p>
        </div>
    );
}

function Pagination({
    page,
    hasMore,
    onPrev,
    onNext,
}: {
    page: number;
    hasMore: boolean;
    onPrev: () => void;
    onNext: () => void;
}) {
    if (page === 0 && !hasMore) return null;
    return (
        <div className="flex items-center justify-between text-sm text-text-muted">
            <span>Page {page + 1}</span>
            <div className="flex gap-2">
                <button
                    type="button"
                    onClick={onPrev}
                    disabled={page === 0}
                    className="rounded-md border border-border px-3 py-1.5 hover:bg-bg-card-hover disabled:cursor-not-allowed disabled:opacity-50"
                >
                    ← Prev
                </button>
                <button
                    type="button"
                    onClick={onNext}
                    disabled={!hasMore}
                    className="rounded-md border border-border px-3 py-1.5 hover:bg-bg-card-hover disabled:cursor-not-allowed disabled:opacity-50"
                >
                    Next →
                </button>
            </div>
        </div>
    );
}
