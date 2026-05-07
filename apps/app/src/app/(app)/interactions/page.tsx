"use client";

/**
 * Global Interactions list — replaces the prior redirect stub. Same
 * drill-down depth as the customer-page Interactions tab; sort + filter
 * lives at the top of the list.
 */

import Link from "next/link";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useApi } from "@/lib/api";
import {
    formatDuration,
    formatRelative,
    sentimentLabel,
    type InteractionOut,
} from "@/lib/interactions";

type SortKey = "newest" | "oldest" | "longest" | "shortest" | "churn_high";

const SORT_LABEL: Record<SortKey, string> = {
    newest: "Newest first",
    oldest: "Oldest first",
    longest: "Longest first",
    shortest: "Shortest first",
    churn_high: "Highest churn risk",
};

export default function GlobalInteractionsPage() {
    const api = useApi();
    const [channel, setChannel] = useState("");
    const [status, setStatus] = useState("");
    const [query, setQuery] = useState("");
    const [sort, setSort] = useState<SortKey>("newest");

    const { data, isLoading, error } = useQuery({
        queryKey: ["interactions", "list", { channel, status, query }],
        queryFn: async () => {
            const params = new URLSearchParams();
            if (channel) params.set("channel", channel);
            if (status) params.set("status", status);
            if (query) params.set("q", query);
            params.set("limit", "200");
            return api.get<InteractionOut[]>(
                `/interactions?${params.toString()}`,
            );
        },
    });

    const items = data ?? [];
    const sortedItems = [...items].sort((a, b) => {
        switch (sort) {
            case "oldest":
                return a.created_at.localeCompare(b.created_at);
            case "longest":
                return (b.duration_seconds ?? 0) - (a.duration_seconds ?? 0);
            case "shortest":
                return (a.duration_seconds ?? 0) - (b.duration_seconds ?? 0);
            case "churn_high":
                return (
                    (b.insights?.churn_risk ?? 0) -
                    (a.insights?.churn_risk ?? 0)
                );
            case "newest":
            default:
                return b.created_at.localeCompare(a.created_at);
        }
    });

    return (
        <div className="space-y-6">
            <div className="flex items-baseline justify-between gap-4">
                <h1 className="text-2xl font-bold">Interactions</h1>
                <span className="text-xs text-text-subtle">
                    {items.length} call{items.length === 1 ? "" : "s"}
                </span>
            </div>

            <div className="flex flex-wrap items-end gap-3 rounded-lg border border-border bg-bg-card p-4">
                <FilterField label="Search">
                    <input
                        type="text"
                        value={query}
                        onChange={(e) => setQuery(e.target.value)}
                        placeholder="Title, transcript, phone…"
                        className="rounded border border-border bg-bg-secondary px-2 py-1 text-sm text-text"
                    />
                </FilterField>
                <FilterField label="Channel">
                    <select
                        value={channel}
                        onChange={(e) => setChannel(e.target.value)}
                        className="rounded border border-border bg-bg-secondary px-2 py-1 text-sm text-text"
                    >
                        <option value="">All</option>
                        <option value="voice">Voice</option>
                        <option value="email">Email</option>
                        <option value="chat">Chat</option>
                    </select>
                </FilterField>
                <FilterField label="Status">
                    <select
                        value={status}
                        onChange={(e) => setStatus(e.target.value)}
                        className="rounded border border-border bg-bg-secondary px-2 py-1 text-sm text-text"
                    >
                        <option value="">All</option>
                        <option value="processing">Processing</option>
                        <option value="analyzed">Analyzed</option>
                        <option value="failed">Failed</option>
                    </select>
                </FilterField>
                <FilterField label="Sort">
                    <select
                        value={sort}
                        onChange={(e) => setSort(e.target.value as SortKey)}
                        className="rounded border border-border bg-bg-secondary px-2 py-1 text-sm text-text"
                    >
                        {(Object.keys(SORT_LABEL) as SortKey[]).map((k) => (
                            <option key={k} value={k}>
                                {SORT_LABEL[k]}
                            </option>
                        ))}
                    </select>
                </FilterField>
            </div>

            {isLoading && (
                <p className="text-sm text-text-subtle">Loading…</p>
            )}
            {error && (
                <p className="text-sm text-accent-rose">
                    Couldn&apos;t load interactions.
                </p>
            )}

            {sortedItems.length === 0 && !isLoading ? (
                <p className="text-sm text-text-subtle">
                    No interactions match these filters.
                </p>
            ) : (
                <ul className="divide-y divide-border-light rounded-lg border border-border bg-bg-card">
                    {sortedItems.map((item) => (
                        <InteractionRow key={item.id} item={item} />
                    ))}
                </ul>
            )}
        </div>
    );
}

function InteractionRow({ item }: { item: InteractionOut }) {
    const sent = sentimentLabel(item.insights?.sentiment_score);
    const churnPct =
        typeof item.insights?.churn_risk === "number"
            ? `${(item.insights.churn_risk * 100).toFixed(0)}%`
            : null;
    return (
        <li>
            <Link
                href={`/interactions/${item.id}`}
                className="block px-4 py-3 hover:bg-card-hover"
            >
                <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0 flex-1">
                        <div className="flex items-baseline gap-2">
                            <span className="truncate text-sm font-medium text-text">
                                {item.title || "(untitled)"}
                            </span>
                            <span className="text-xs uppercase tracking-wide text-text-subtle">
                                {item.channel}
                            </span>
                        </div>
                        <div className="mt-1 text-xs text-text-muted">
                            {formatRelative(item.created_at)}
                            {item.duration_seconds != null && (
                                <>
                                    {" · "}
                                    {formatDuration(item.duration_seconds)}
                                </>
                            )}
                            {sent && (
                                <>
                                    {" · "}
                                    {sent}
                                </>
                            )}
                            {churnPct && (
                                <>
                                    {" · "}
                                    <span className="text-accent-rose">
                                        Churn {churnPct}
                                    </span>
                                </>
                            )}
                        </div>
                    </div>
                    <span className="rounded-full border border-border bg-bg-secondary px-2 py-0.5 text-xs capitalize text-text-muted">
                        {item.status}
                    </span>
                </div>
            </Link>
        </li>
    );
}

function FilterField({
    label,
    children,
}: {
    label: string;
    children: React.ReactNode;
}) {
    return (
        <label className="flex flex-col gap-1 text-xs text-text-muted">
            {label}
            {children}
        </label>
    );
}
