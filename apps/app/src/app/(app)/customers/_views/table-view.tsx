"use client";

import Link from "next/link";
import { type CustomerListItem } from "@/lib/customers";
import { formatRelative, sentimentLabel } from "@/lib/interactions";
import { ChurnTone, CustomerLogo, OwnerStack } from "./shared";

/** View 1 — Sortable table. Densest information per pixel. */
export function CustomerTableView({ items }: { items: CustomerListItem[] }) {
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
                        <Row key={c.id} c={c} />
                    ))}
                </tbody>
            </table>
        </div>
    );
}

function Row({ c }: { c: CustomerListItem }) {
    const sent = sentimentLabel(c.sentiment_score);
    const churn = ChurnTone(c.churn_risk);
    return (
        <tr className="hover:bg-bg-card-hover">
            <td className="px-4 py-3">
                <Link
                    href={`/customers/${c.id}`}
                    className="flex items-center gap-3 group"
                >
                    <CustomerLogo domain={c.domain} name={c.name} />
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
                    <span className="text-text-subtle">-</span>
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
                    <span className="text-text-subtle">-</span>
                )}
            </td>
            <td className={`px-4 py-3 ${churn.cls}`}>{churn.label}</td>
            <td className="px-4 py-3">
                <span className="text-text">{c.multithreading_90d}</span>
                <span className="ml-1 text-xs text-text-subtle">/ 90d</span>
            </td>
            <td className="px-4 py-3">{c.open_action_items}</td>
        </tr>
    );
}
