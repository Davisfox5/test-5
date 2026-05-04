"use client";

import { type CustomerListItem } from "@/lib/customers";
import { formatRelative, sentimentLabel } from "@/lib/interactions";
import { ChurnTone, CustomerCardLink, CustomerLogo, OwnerStack } from "./shared";

/** View 2 — Card grid. ~6–9 cards visible at once. */
export function CustomerGridView({ items }: { items: CustomerListItem[] }) {
    return (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {items.map((c) => (
                <Card key={c.id} c={c} />
            ))}
        </div>
    );
}

function Card({ c }: { c: CustomerListItem }) {
    const sent = sentimentLabel(c.sentiment_score);
    const churn = ChurnTone(c.churn_risk);
    return (
        <CustomerCardLink c={c}>
            <div className="flex items-start justify-between gap-3">
                <div className="flex min-w-0 items-center gap-3">
                    <CustomerLogo domain={c.domain} name={c.name} size={40} />
                    <div className="min-w-0">
                        <div className="truncate font-semibold text-text">
                            {c.name}
                        </div>
                        <div className="truncate text-xs text-text-subtle">
                            {c.domain ?? "no domain yet"}
                        </div>
                    </div>
                </div>
                <OwnerStack owners={c.owners} />
            </div>

            <div className="mt-4 grid grid-cols-3 gap-2 text-center text-xs">
                <Stat
                    label="Sentiment"
                    value={
                        c.sentiment_score != null
                            ? c.sentiment_score.toFixed(1)
                            : "—"
                    }
                    tone={sent.tone}
                />
                <Stat
                    label="Churn"
                    value={churn.label}
                    customClass={churn.cls}
                />
                <Stat
                    label="Threads"
                    value={`${c.multithreading_90d}`}
                    tone="subtle"
                />
            </div>

            <div className="mt-3 flex items-baseline justify-between gap-2 text-xs text-text-muted">
                <span className="truncate">
                    {c.latest_interaction_at
                        ? formatRelative(c.latest_interaction_at)
                        : "no activity yet"}
                </span>
                <span className="shrink-0">
                    {c.open_action_items} open item
                    {c.open_action_items === 1 ? "" : "s"}
                </span>
            </div>
            {c.latest_interaction_title ? (
                <p className="mt-1 line-clamp-2 text-xs text-text-subtle">
                    {c.latest_interaction_title}
                </p>
            ) : null}
        </CustomerCardLink>
    );
}

function Stat({
    label,
    value,
    tone,
    customClass,
}: {
    label: string;
    value: string;
    tone?: "emerald" | "amber" | "rose" | "subtle";
    customClass?: string;
}) {
    const cls =
        customClass ??
        (tone === "emerald"
            ? "text-accent-emerald"
            : tone === "amber"
              ? "text-accent-amber"
              : tone === "rose"
                ? "text-accent-rose"
                : "text-text");
    return (
        <div className="rounded-md border border-border bg-bg-secondary px-2 py-1">
            <div className="text-[10px] uppercase tracking-wide text-text-subtle">
                {label}
            </div>
            <div className={`font-semibold ${cls}`}>{value}</div>
        </div>
    );
}
