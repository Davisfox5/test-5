"use client";

/**
 * View 4 — Kanban by lifecycle stage.
 *
 * Lifecycle stages ship in Phase 5 (the per-tenant taxonomy + AI-
 * derived transitions). Until then the Kanban view is a single
 * "All customers" column with a banner explaining where stages will
 * come from. Shipping this stub now keeps the four-view A/B test
 * intact and avoids a blank tab; it'll bloom into a real Kanban
 * when stages land.
 */

import Link from "next/link";
import { type CustomerListItem } from "@/lib/customers";
import { ChurnTone, CustomerLogo, OwnerStack } from "./shared";

export function CustomerKanbanView({
    items,
}: {
    items: CustomerListItem[];
}) {
    return (
        <div className="space-y-4">
            <div className="rounded-md border border-accent-amber/40 bg-accent-amber/5 px-4 py-3 text-xs text-text-muted">
                <strong className="text-accent-amber">Coming with stages:</strong>{" "}
                Kanban groups customers by lifecycle stage (Lead / Trial /
                Customer / Renewal due / At risk). Stages land in a later
                phase. For now, every customer renders in a single column.
            </div>
            <div className="grid grid-cols-1 gap-4 lg:grid-cols-1">
                <Column title="All customers" items={items} />
            </div>
        </div>
    );
}

function Column({
    title,
    items,
}: {
    title: string;
    items: CustomerListItem[];
}) {
    return (
        <section className="rounded-lg border border-border bg-bg-secondary p-3">
            <header className="mb-2 flex items-baseline justify-between">
                <h3 className="text-sm font-semibold">{title}</h3>
                <span className="text-xs text-text-subtle">
                    {items.length}
                </span>
            </header>
            <div className="space-y-2">
                {items.map((c) => (
                    <Card key={c.id} c={c} />
                ))}
            </div>
        </section>
    );
}

function Card({ c }: { c: CustomerListItem }) {
    const churn = ChurnTone(c.churn_risk);
    return (
        <Link
            href={`/customers/${c.id}`}
            className="block rounded-md border border-border bg-bg-card p-3 hover:bg-bg-card-hover"
        >
            <div className="flex items-start justify-between gap-2">
                <div className="flex min-w-0 items-center gap-2">
                    <CustomerLogo
                        domain={c.domain}
                        name={c.name}
                        size={28}
                    />
                    <div className="min-w-0">
                        <div className="truncate text-sm font-medium text-text">
                            {c.name}
                        </div>
                        <div
                            className={`text-xs ${churn.cls}`}
                        >
                            churn {churn.label}
                        </div>
                    </div>
                </div>
                <OwnerStack owners={c.owners} />
            </div>
            <div className="mt-2 flex justify-between text-xs text-text-subtle">
                <span>{c.open_action_items} open</span>
                <span>{c.multithreading_90d}/90d</span>
            </div>
        </Link>
    );
}
