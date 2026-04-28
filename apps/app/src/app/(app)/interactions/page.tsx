"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import { UploadModal } from "@/components/upload-modal";
import {
    formatDuration,
    formatRelative,
    sentimentLabel,
    useInteractions,
    type InteractionOut,
} from "@/lib/interactions";

const PAGE_SIZE = 25;

export default function InteractionsPage() {
    const [page, setPage] = useState(0);
    const [q, setQ] = useState("");
    const [status, setStatus] = useState("");
    const [from, setFrom] = useState("");
    const [to, setTo] = useState("");
    const [uploadOpen, setUploadOpen] = useState(false);

    const list = useInteractions({
        limit: PAGE_SIZE,
        offset: page * PAGE_SIZE,
        q: q || undefined,
        status: status || undefined,
    });

    const filteredRows = useMemo(() => {
        const rows = list.data ?? [];
        if (!from && !to) return rows;
        // Backend doesn't accept date-range filters yet; clip locally so
        // the UI honors the picker until the endpoint grows them.
        const fromTs = from ? new Date(from).getTime() : -Infinity;
        const toTs = to ? new Date(to).getTime() + 86_400_000 : Infinity;
        return rows.filter((r) => {
            const t = new Date(r.created_at).getTime();
            return t >= fromTs && t < toTs;
        });
    }, [list.data, from, to]);

    const isFiltered = !!(q || status || from || to);
    const isEmpty =
        !list.isLoading &&
        !list.error &&
        filteredRows.length === 0 &&
        !isFiltered;

    return (
        <div className="space-y-4">
            <header className="flex items-start justify-between gap-4">
                <div>
                    <h2 className="text-2xl font-bold">Interactions</h2>
                    <p className="text-text-muted mt-1">
                        Every call, email, and chat Linda has ingested.
                    </p>
                </div>
                <button
                    type="button"
                    onClick={() => setUploadOpen(true)}
                    className="rounded-md bg-primary px-3 py-2 text-sm font-medium text-white hover:bg-primary-hover"
                >
                    + Upload call
                </button>
            </header>

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
                            <option value="">All</option>
                            <option value="new">New</option>
                            <option value="processing">Processing</option>
                            <option value="analyzed">Analyzed</option>
                            <option value="failed">Failed</option>
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
                    <EmptyState onUpload={() => setUploadOpen(true)} />
                ) : filteredRows.length === 0 ? (
                    <p className="px-6 py-8 text-center text-sm text-text-muted">
                        No interactions match your filters.
                    </p>
                ) : (
                    <Table rows={filteredRows} />
                )}
            </section>

            <Pagination
                page={page}
                hasMore={(list.data?.length ?? 0) === PAGE_SIZE}
                onPrev={() => setPage((p) => Math.max(0, p - 1))}
                onNext={() => setPage((p) => p + 1)}
            />

            <UploadModal
                open={uploadOpen}
                onClose={() => setUploadOpen(false)}
            />
        </div>
    );
}

/* ── Table ──────────────────────────────────────────────────────────── */

function Table({ rows }: { rows: InteractionOut[] }) {
    return (
        <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
                <thead className="border-b border-border bg-bg-secondary">
                    <tr className="text-xs uppercase tracking-wide text-text-subtle">
                        <Th>Date</Th>
                        <Th>Customer</Th>
                        <Th>Duration</Th>
                        <Th>Sentiment</Th>
                        <Th>Action items</Th>
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
    const aiCount =
        (row.call_metrics?.action_items_count as number | undefined) ??
        (row.insights?.action_items_count as number | undefined) ??
        0;
    return (
        <tr
            className="cursor-pointer border-b border-border last:border-b-0 hover:bg-bg-card-hover"
            onClick={() => {
                window.location.href = `/interactions/${row.id}`;
            }}
        >
            <Td>
                <Link
                    href={`/interactions/${row.id}`}
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
            <Td>{aiCount}</Td>
            <Td>
                <span className="rounded-full border border-border px-2 py-0.5 text-xs capitalize text-text-muted">
                    {row.status}
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

function EmptyState({ onUpload }: { onUpload: () => void }) {
    return (
        <div className="px-6 py-16 text-center">
            <p className="text-base font-semibold">No calls yet</p>
            <p className="mx-auto mt-2 max-w-md text-sm text-text-muted">
                Upload a recording or paste a URL — Linda transcribes and
                analyzes it, then you&apos;ll see it here.
            </p>
            <button
                type="button"
                onClick={onUpload}
                className="mt-5 inline-flex rounded-md bg-primary px-4 py-2 text-sm font-medium text-white hover:bg-primary-hover"
            >
                Upload your first call
            </button>
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
