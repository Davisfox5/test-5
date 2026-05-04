"use client";

/**
 * Customers list page — Phase 3C wraps Phase 3A.
 *
 * Four switchable views (table / grid / hybrid / kanban) behind a
 * top-of-page tab switcher. Default lands on the table view; per
 * the user's planning answer the four are running in parallel for
 * comparison until they pick one. Sort + filter + search apply to
 * all views — the data shape is identical.
 *
 * Kanban is a stub for now (single column) because lifecycle stages
 * haven't shipped; it bloomes into real columns when Phase 5 lands.
 */

import { useState } from "react";
import {
    useCustomerList,
    type CustomerListSort,
} from "@/lib/customers";
import { CustomerGridView } from "./_views/grid-view";
import { CustomerHybridView } from "./_views/hybrid-view";
import { CustomerKanbanView } from "./_views/kanban-view";
import { CustomerTableView } from "./_views/table-view";

type ViewKey = "table" | "grid" | "hybrid" | "kanban";

const VIEW_LABEL: Record<ViewKey, string> = {
    table: "Table",
    grid: "Grid",
    hybrid: "Hybrid",
    kanban: "Kanban",
};

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
    const [view, setView] = useState<ViewKey>("table");
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
                <div
                    role="tablist"
                    aria-label="Customer list view"
                    className="ml-auto flex items-center gap-1 rounded-md border border-border bg-bg-secondary p-1"
                >
                    {(["table", "grid", "hybrid", "kanban"] as const).map(
                        (k) => (
                            <button
                                key={k}
                                type="button"
                                role="tab"
                                aria-selected={view === k}
                                onClick={() => setView(k)}
                                className={`rounded px-3 py-1 text-xs font-medium transition-colors ${
                                    view === k
                                        ? "bg-primary text-white"
                                        : "text-text-muted hover:text-text"
                                }`}
                            >
                                {VIEW_LABEL[k]}
                            </button>
                        ),
                    )}
                </div>
                <span className="text-xs text-text-subtle">
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
            ) : view === "table" ? (
                <CustomerTableView items={items} />
            ) : view === "grid" ? (
                <CustomerGridView items={items} />
            ) : view === "hybrid" ? (
                <CustomerHybridView items={items} />
            ) : (
                <CustomerKanbanView items={items} />
            )}
        </div>
    );
}
