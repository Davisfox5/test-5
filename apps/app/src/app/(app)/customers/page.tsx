"use client";

/**
 * Customers list page — Phase 3A.
 *
 * Renders the table view as the MVP. Grid, kanban, and hybrid variants
 * land in Phase 3C. Default sort is latest_interaction (matches
 * "what changed recently" mental model); a sort selector lets the user
 * switch to name / churn risk / open items / multithreading.
 *
 * The data shape comes from /api/v1/customers/list (CustomerListItem)
 * which gives us per-row owners, multithreading count, latest
 * interaction summary, sentiment, churn, and open-action-item count
 * — everything the row needs without extra fetches.
 */

import Link from "next/link";
import Image from "next/image";
import { useMemo, useState } from "react";
import {
    faviconFor,
    useCustomerList,
    type CustomerListItem,
    type CustomerListSort,
} from "@/lib/customers";
import { formatRelative, sentimentLabel } from "@/lib/interactions";

const SORT_OPTIONS: { value: CustomerListSort; label: string }[] = [
    { value: "latest_interaction", label: "Latest activity" },
    { value: "name", label: "Name" },
    { value: "churn_risk", label: "Churn risk" },
    { value: "open_action_items", label: "Open action items" },
    { value: "multithreading_90d", label: "Multithreading (90d)" },
];

export default function CustomersPage() {
    const [sort, setSort] = useState<CustomerListSort>("latest_interaction");
    const [nameFilter, setNameFilter] = useState("");
    const list = useCustomerList({ sort, name: nameFilter || undefined });

    const items = list.data?.items ?? [];
    const total = list.data?.total ?? 0;

    return (
        <div className="space-y-6">
            <header>
                <h1 className="text-2xl font-bold">Customers</h1>
                <p className="text-sm text-text-muted">
                    Every account Linda has identified from your calls. Click
                    a row to see the full record, including the conversation
                    history and open action items.
                </p>
            </header>

            <div className="flex flex-wrap items-center gap-3">
                <input
                    type="search"
                    value={nameFilter}
                    onChange={(e) => setNameFilter(e.target.value)}
                    placeholder="Filter by name…"
                    className="w-64 rounded-md border border-border bg-bg-secondary px-3 py-2 text-sm outline-none focus:border-primary"
                />
                <label className="flex items-center gap-2 text-sm text-text-muted">
                    Sort by
                    <select
                        value={sort}
                        onChange={(e) => setSort(e.target.value as CustomerListSort)}
                        className="rounded-md border border-border bg-bg-secondary px-2 py-1 text-sm"
                    >
                        {SORT_OPTIONS.map((opt) => (
                            <option key={opt.value} value={opt.value}>
                                {opt.label}
                            </option>
                        ))}
                    </select>
                </label>
                <span className="ml-auto text-xs text-text-subtle">
                    {total} customer{total === 1 ? "" : "s"}
                </span>
            </div>

            {list.isLoading ? (
                <div className="rounded-lg border border-border bg-bg-card p-6 text-sm text-text-muted">
                    Loading customers…
                </div>
            ) : list.error ? (
                <div className="rounded-lg border border-accent-rose/40 bg-bg-card p-6 text-sm text-accent-rose">
                    Couldn&apos;t load customers.{" "}
                    {list.error instanceof Error ? list.error.message : ""}
                </div>
            ) : items.length === 0 ? (
                <div className="rounded-lg border border-border bg-bg-card p-6 text-sm text-text-muted">
                    No customers yet. Linda creates a customer record from each
                    analyzed call. Upload a call or wait for ingestion to
                    finish; they&apos;ll appear here automatically.
                </div>
            ) : (
                <CustomerTable items={items} />
            )}
        </div>
    );
}

function CustomerTable({ items }: { items: CustomerListItem[] }) {
    return (
        <div className="overflow-x-auto rounded-lg border border-border bg-bg-card">
            <table className="w-full text-sm">
                <thead className="border-b border-border bg-bg-secondary text-xs uppercase tracking-wide text-text-subtle">
                    <tr>
                        <th className="px-4 py-2 text-left">Customer</th>
                        <th className="px-4 py-2 text-left">Owners</th>
                        <th className="px-4 py-2 text-left">Last activity</th>
                        <th className="px-4 py-2 text-left">Sentiment</th>
                        <th className="px-4 py-2 text-left">Churn</th>
                        <th className="px-4 py-2 text-left">Threads</th>
                        <th className="px-4 py-2 text-left">Open items</th>
                    </tr>
                </thead>
                <tbody className="divide-y divide-border">
                    {items.map((c) => (
                        <CustomerRow key={c.id} c={c} />
                    ))}
                </tbody>
            </table>
        </div>
    );
}

function CustomerRow({ c }: { c: CustomerListItem }) {
    const sent = sentimentLabel(c.sentiment_score);
    const churnPct =
        c.churn_risk != null ? `${Math.round(c.churn_risk * 100)}%` : "—";
    const churnTone =
        c.churn_risk == null
            ? "text-text-subtle"
            : c.churn_risk >= 0.7
              ? "text-accent-rose"
              : c.churn_risk >= 0.4
                ? "text-accent-amber"
                : "text-accent-emerald";
    const fav = faviconFor(c.domain);

    return (
        <tr className="hover:bg-bg-card-hover">
            <td className="px-4 py-3">
                <Link
                    href={`/customers/${c.id}`}
                    className="flex items-center gap-3 group"
                >
                    <CustomerLogo domain={c.domain} fav={fav} name={c.name} />
                    <div className="min-w-0">
                        <div className="truncate font-medium text-text group-hover:text-primary">
                            {c.name}
                        </div>
                        <div className="truncate text-xs text-text-subtle">
                            {c.domain ?? "no domain yet"}
                        </div>
                    </div>
                </Link>
            </td>
            <td className="px-4 py-3">
                <OwnerStack owners={c.owners} />
            </td>
            <td className="px-4 py-3">
                {c.latest_interaction_at ? (
                    <Link
                        href={
                            c.latest_interaction_id
                                ? `/interactions/${c.latest_interaction_id}`
                                : "#"
                        }
                        className="block hover:text-primary"
                    >
                        <div className="text-text">
                            {formatRelative(c.latest_interaction_at)}
                        </div>
                        <div className="truncate text-xs text-text-subtle max-w-[18rem]">
                            {c.latest_interaction_title ?? ""}
                        </div>
                    </Link>
                ) : (
                    <span className="text-text-subtle">—</span>
                )}
            </td>
            <td className="px-4 py-3">
                {c.sentiment_score != null ? (
                    <div>
                        <div className="font-medium">
                            {c.sentiment_score.toFixed(1)}
                        </div>
                        <div
                            className={`text-xs ${
                                sent.tone === "emerald"
                                    ? "text-accent-emerald"
                                    : sent.tone === "amber"
                                      ? "text-accent-amber"
                                      : sent.tone === "rose"
                                        ? "text-accent-rose"
                                        : "text-text-subtle"
                            }`}
                        >
                            {sent.text}
                        </div>
                    </div>
                ) : (
                    <span className="text-text-subtle">—</span>
                )}
            </td>
            <td className={`px-4 py-3 ${churnTone}`}>{churnPct}</td>
            <td className="px-4 py-3">
                <span className="text-text">{c.multithreading_90d}</span>
                <span className="ml-1 text-xs text-text-subtle">/ 90d</span>
            </td>
            <td className="px-4 py-3">{c.open_action_items}</td>
        </tr>
    );
}

function CustomerLogo({
    domain,
    fav,
    name,
}: {
    domain: string | null;
    fav: string | null;
    name: string;
}) {
    const initials = useMemo(() => {
        const tokens = name
            .split(/\s+/)
            .filter(Boolean)
            .slice(0, 2)
            .map((t) => t[0]?.toUpperCase() ?? "");
        return tokens.join("") || "·";
    }, [name]);

    if (fav) {
        // Next/Image needs an explicit width/height for unoptimised
        // external sources. We mark unoptimized to avoid Next baking the
        // domain into its allowlist — Google's favicon service is
        // ubiquitous enough that maintaining the allowlist is overhead.
        return (
            <div className="relative h-8 w-8 shrink-0 overflow-hidden rounded-md bg-bg-secondary">
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img
                    src={fav}
                    alt={`${domain ?? name} logo`}
                    className="h-8 w-8 object-cover"
                    loading="lazy"
                />
            </div>
        );
    }
    return (
        <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-primary-soft text-xs font-semibold text-primary">
            {initials}
        </div>
    );
}

function OwnerStack({
    owners,
}: {
    owners: CustomerListItem["owners"];
}) {
    if (owners.length === 0) {
        return <span className="text-xs text-text-subtle">unassigned</span>;
    }
    const visible = owners.slice(0, 3);
    const extra = owners.length - visible.length;
    return (
        <div className="flex items-center">
            {visible.map((o, idx) => (
                <div
                    key={o.user_id}
                    title={`${o.name ?? o.email ?? "Unknown"} (${o.role})`}
                    className={`flex h-7 w-7 items-center justify-center rounded-full border-2 border-bg-card text-xs font-medium ${
                        o.role === "primary"
                            ? "bg-primary text-white"
                            : "bg-bg-secondary text-text-muted"
                    } ${idx > 0 ? "-ml-2" : ""}`}
                >
                    {(o.name || o.email || "?").charAt(0).toUpperCase()}
                </div>
            ))}
            {extra > 0 ? (
                <div className="-ml-2 flex h-7 w-7 items-center justify-center rounded-full border-2 border-bg-card bg-bg-secondary text-xs text-text-muted">
                    +{extra}
                </div>
            ) : null}
        </div>
    );
}
