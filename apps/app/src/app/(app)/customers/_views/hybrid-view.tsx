"use client";

import { type CustomerListItem } from "@/lib/customers";
import { CustomerGridView } from "./grid-view";
import { CustomerTableView } from "./table-view";

/**
 * View 3 — Hybrid.
 *
 * Top "Needs attention" cards (high churn, recent activity, overdue
 * action items) plus a full sortable table below. Daily-driver shape:
 * triage at the top, completeness underneath.
 */
export function CustomerHybridView({
    items,
}: {
    items: CustomerListItem[];
}) {
    const attention = needsAttention(items);
    return (
        <div className="space-y-6">
            {attention.length > 0 ? (
                <section>
                    <header className="mb-3 flex items-baseline justify-between">
                        <h2 className="text-sm font-semibold">
                            Needs attention
                        </h2>
                        <span className="text-xs text-text-subtle">
                            high-churn customers · stale accounts ·
                            heavy open-item load
                        </span>
                    </header>
                    <CustomerGridView items={attention.slice(0, 6)} />
                </section>
            ) : null}
            <section>
                <header className="mb-3">
                    <h2 className="text-sm font-semibold">All customers</h2>
                </header>
                <CustomerTableView items={items} />
            </section>
        </div>
    );
}

/**
 * Pick customers worth flagging, in priority order.
 *
 * The signal stack: high churn risk first; then accounts with no
 * activity in 30+ days where there are open action items (stale
 * obligations); then accounts with the most open items (load).
 * Capped at the top 6 — anything more belongs in the table.
 */
function needsAttention(items: CustomerListItem[]): CustomerListItem[] {
    const now = Date.now();
    const score = (c: CustomerListItem): number => {
        const churnPart = (c.churn_risk ?? 0) * 100;
        const lastDays = c.latest_interaction_at
            ? (now - new Date(c.latest_interaction_at).getTime()) /
              (1000 * 60 * 60 * 24)
            : 0;
        const stalePart =
            lastDays > 30 && c.open_action_items > 0 ? 30 : 0;
        const loadPart = Math.min(c.open_action_items * 2, 20);
        return churnPart + stalePart + loadPart;
    };
    return [...items]
        .filter((c) => score(c) > 30)
        .sort((a, b) => score(b) - score(a));
}
