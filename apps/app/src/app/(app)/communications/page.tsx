"use client";

import Link from "next/link";
import { useMemo, useState } from "react";

import {
    EmailSendListItem,
    type CommunicationsListFilters,
    useCommunicationsList,
} from "@/lib/communications";

type StatusFilter = "all" | "sent" | "failed" | "pending";

const PAGE_SIZE = 25;

const STATUS_TABS: { key: StatusFilter; label: string }[] = [
    { key: "all", label: "All" },
    { key: "sent", label: "Sent" },
    { key: "failed", label: "Failed" },
    { key: "pending", label: "Pending" },
];

function fmtDateTime(value: string | null | undefined): string {
    if (!value) return "—";
    const d = new Date(value);
    if (Number.isNaN(d.getTime())) return "—";
    return d.toLocaleString();
}

function truncate(text: string, max = 80): string {
    if (text.length <= max) return text;
    return `${text.slice(0, max - 1)}…`;
}

export default function CommunicationsPage() {
    const [statusTab, setStatusTab] = useState<StatusFilter>("all");
    const [dateFrom, setDateFrom] = useState("");
    const [dateTo, setDateTo] = useState("");
    const [q, setQ] = useState("");
    const [page, setPage] = useState(0);
    const [expandedId, setExpandedId] = useState<string | null>(null);

    const filters: CommunicationsListFilters = useMemo(() => {
        const f: CommunicationsListFilters = {
            limit: PAGE_SIZE,
            offset: page * PAGE_SIZE,
        };
        if (statusTab !== "all") f.status = statusTab;
        if (dateFrom) f.dateFrom = dateFrom;
        if (dateTo) f.dateTo = dateTo;
        if (q) f.q = q;
        return f;
    }, [statusTab, dateFrom, dateTo, q, page]);

    const { data, isLoading, error } = useCommunicationsList(filters);

    const items = data?.items ?? [];
    const total = data?.total ?? 0;
    const hasNext = (page + 1) * PAGE_SIZE < total;
    const hasPrev = page > 0;

    return (
        <div className="space-y-6">
            <header>
                <h2 className="text-2xl font-bold">Communications</h2>
                <p className="text-text-muted mt-1">
                    Follow-ups Linda has sent or queued for your tenant.
                </p>
            </header>

            <section className="rounded-lg border border-border bg-bg-card p-4">
                <div className="flex flex-wrap items-end gap-3">
                    <div className="inline-flex rounded-lg border border-border bg-bg-secondary p-1">
                        {STATUS_TABS.map((t) => (
                            <button
                                key={t.key}
                                type="button"
                                onClick={() => {
                                    setStatusTab(t.key);
                                    setPage(0);
                                }}
                                className={`px-3 py-1.5 text-sm rounded-md transition ${
                                    statusTab === t.key
                                        ? "bg-primary text-white"
                                        : "text-text-muted hover:text-text"
                                }`}
                            >
                                {t.label}
                            </button>
                        ))}
                    </div>
                    <label className="flex flex-col gap-1 text-sm">
                        <span className="text-xs uppercase tracking-wide text-text-subtle">
                            From
                        </span>
                        <input
                            type="date"
                            value={dateFrom}
                            onChange={(e) => {
                                setDateFrom(e.target.value);
                                setPage(0);
                            }}
                            className="rounded-md border border-border bg-bg-elevated px-3 py-1.5 text-sm"
                        />
                    </label>
                    <label className="flex flex-col gap-1 text-sm">
                        <span className="text-xs uppercase tracking-wide text-text-subtle">
                            To
                        </span>
                        <input
                            type="date"
                            value={dateTo}
                            onChange={(e) => {
                                setDateTo(e.target.value);
                                setPage(0);
                            }}
                            className="rounded-md border border-border bg-bg-elevated px-3 py-1.5 text-sm"
                        />
                    </label>
                    <label className="flex flex-1 min-w-[200px] flex-col gap-1 text-sm">
                        <span className="text-xs uppercase tracking-wide text-text-subtle">
                            Search recipient or subject
                        </span>
                        <input
                            type="text"
                            value={q}
                            onChange={(e) => {
                                setQ(e.target.value);
                                setPage(0);
                            }}
                            placeholder="sarah@…"
                            className="rounded-md border border-border bg-bg-elevated px-3 py-1.5 text-sm"
                        />
                    </label>
                </div>
            </section>

            {error ? (
                <div className="rounded-lg border border-accent-rose bg-bg-card p-4 text-sm text-accent-rose">
                    Couldn&apos;t load communications:{" "}
                    {(error as Error).message}
                </div>
            ) : isLoading && !data ? (
                <Skeleton />
            ) : items.length === 0 ? (
                <EmptyState />
            ) : (
                <section className="rounded-lg border border-border bg-bg-card overflow-hidden">
                    <table className="w-full text-sm">
                        <thead className="bg-bg-secondary text-text-subtle text-xs uppercase tracking-wide">
                            <tr>
                                <th className="px-4 py-2 text-left">
                                    Sent at
                                </th>
                                <th className="px-4 py-2 text-left">To</th>
                                <th className="px-4 py-2 text-left">Subject</th>
                                <th className="px-4 py-2 text-left">
                                    Interaction
                                </th>
                                <th className="px-4 py-2 text-left">Sender</th>
                                <th className="px-4 py-2 text-left">Status</th>
                            </tr>
                        </thead>
                        <tbody>
                            {items.map((item) => (
                                <Row
                                    key={item.id}
                                    item={item}
                                    expanded={expandedId === item.id}
                                    onToggle={() =>
                                        setExpandedId((cur) =>
                                            cur === item.id ? null : item.id,
                                        )
                                    }
                                />
                            ))}
                        </tbody>
                    </table>
                </section>
            )}

            {items.length > 0 ? (
                <div className="flex items-center justify-between text-xs text-text-subtle">
                    <span>
                        {page * PAGE_SIZE + 1}–
                        {Math.min((page + 1) * PAGE_SIZE, total)} of {total}
                    </span>
                    <div className="flex gap-2">
                        <button
                            type="button"
                            onClick={() => setPage((p) => Math.max(0, p - 1))}
                            disabled={!hasPrev}
                            className="rounded-md border border-border px-3 py-1 hover:bg-bg-secondary disabled:opacity-50"
                        >
                            Prev
                        </button>
                        <button
                            type="button"
                            onClick={() => setPage((p) => p + 1)}
                            disabled={!hasNext}
                            className="rounded-md border border-border px-3 py-1 hover:bg-bg-secondary disabled:opacity-50"
                        >
                            Next
                        </button>
                    </div>
                </div>
            ) : null}
        </div>
    );
}

function Row({
    item,
    expanded,
    onToggle,
}: {
    item: EmailSendListItem;
    expanded: boolean;
    onToggle: () => void;
}) {
    return (
        <>
            <tr
                onClick={onToggle}
                className="cursor-pointer border-t border-border hover:bg-bg-secondary"
            >
                <td className="px-4 py-3 text-text-muted">
                    {fmtDateTime(item.sent_at ?? item.created_at)}
                </td>
                <td className="px-4 py-3">{item.to_address}</td>
                <td className="px-4 py-3 font-medium">
                    {truncate(item.subject)}
                </td>
                <td
                    className="px-4 py-3"
                    onClick={(e) => e.stopPropagation()}
                >
                    {item.interaction_id ? (
                        <Link
                            href={`/interactions/${item.interaction_id}`}
                            className="text-primary hover:underline"
                        >
                            {item.interaction_title
                                ? truncate(item.interaction_title, 40)
                                : "Open call"}
                        </Link>
                    ) : (
                        <span className="text-text-subtle">—</span>
                    )}
                </td>
                <td className="px-4 py-3 text-text-muted">
                    {item.sender_name ?? item.sender_email ?? "—"}
                </td>
                <td className="px-4 py-3">
                    <StatusPill status={item.status} />
                </td>
            </tr>
            {expanded ? (
                <tr className="border-t border-border bg-bg-secondary">
                    <td colSpan={6} className="px-4 py-3 text-sm">
                        <div className="space-y-3">
                            <div className="grid grid-cols-1 gap-2 text-xs text-text-muted md:grid-cols-3">
                                <div>
                                    <span className="text-text-subtle">
                                        Provider:{" "}
                                    </span>
                                    <span className="capitalize">
                                        {item.provider}
                                    </span>
                                </div>
                                <div>
                                    <span className="text-text-subtle">
                                        Recipient:{" "}
                                    </span>
                                    {item.to_address}
                                </div>
                                {item.cc_address ? (
                                    <div>
                                        <span className="text-text-subtle">
                                            CC:{" "}
                                        </span>
                                        {item.cc_address}
                                    </div>
                                ) : null}
                            </div>
                            <div>
                                <div className="text-xs uppercase tracking-wide text-text-subtle">
                                    Subject
                                </div>
                                <p className="mt-1 font-medium">
                                    {item.subject}
                                </p>
                            </div>
                            <div>
                                <div className="text-xs uppercase tracking-wide text-text-subtle">
                                    Body
                                </div>
                                <pre className="mt-1 whitespace-pre-wrap rounded-md border border-border bg-bg-card p-3 font-sans text-sm text-text-muted">
                                    {item.body}
                                </pre>
                            </div>
                            {item.error ? (
                                <div className="rounded-md border border-accent-rose/40 bg-accent-rose/10 p-3 text-xs text-accent-rose">
                                    Provider error: {item.error}
                                </div>
                            ) : null}
                        </div>
                    </td>
                </tr>
            ) : null}
        </>
    );
}

function StatusPill({ status }: { status: string }) {
    const tone =
        status === "sent"
            ? "text-accent-emerald"
            : status === "failed"
              ? "text-accent-rose"
              : status === "pending"
                ? "text-accent-amber"
                : "text-text-subtle";
    return (
        <span className={`text-xs font-medium capitalize ${tone}`}>
            {status}
        </span>
    );
}

function Skeleton() {
    return (
        <div className="rounded-lg border border-border bg-bg-card p-6 animate-pulse">
            <div className="h-4 w-40 bg-bg-secondary rounded mb-3" />
            <div className="h-3 w-2/3 bg-bg-secondary rounded mb-2" />
            <div className="h-3 w-1/2 bg-bg-secondary rounded" />
        </div>
    );
}

function EmptyState() {
    return (
        <div className="rounded-lg border border-border border-dashed bg-bg-card p-10 text-center space-y-2">
            <p className="text-text-muted">
                No follow-ups sent yet.
            </p>
            <p className="text-text-subtle text-sm">
                Send one from any interaction&apos;s detail page —{" "}
                <Link
                    href="/interactions"
                    className="text-primary hover:underline"
                >
                    open /interactions
                </Link>{" "}
                to pick a call.
            </p>
        </div>
    );
}
