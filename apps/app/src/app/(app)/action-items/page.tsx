"use client";

import Link from "next/link";
import { useMemo, useState } from "react";

import {
    ActionItem,
    ActionItemFilters,
    useActionItems,
    useTenantUsers,
    useUpdateActionItem,
} from "@/lib/action-items";

type Tab = "open" | "done" | "snoozed";

const TABS: { key: Tab; label: string; status: string }[] = [
    { key: "open", label: "Open", status: "pending" },
    { key: "done", label: "Done", status: "done" },
    { key: "snoozed", label: "Snoozed", status: "snoozed" },
];

const DONE_STATUSES = new Set(["done", "completed"]);

function statusForTab(tab: Tab): string {
    return TABS.find((t) => t.key === tab)?.status ?? "pending";
}

function fmtDate(value: string | null | undefined): string {
    if (!value) return "—";
    const d = new Date(value);
    if (Number.isNaN(d.getTime())) return "—";
    return d.toLocaleDateString();
}

function inOneDayISODate(): string {
    const d = new Date();
    d.setDate(d.getDate() + 1);
    return d.toISOString().slice(0, 10);
}

export default function ActionItemsPage() {
    const [tab, setTab] = useState<Tab>("open");
    const [q, setQ] = useState("");
    const [assignee, setAssignee] = useState<string>("");
    const [dueFrom, setDueFrom] = useState<string>("");
    const [dueTo, setDueTo] = useState<string>("");
    const [expandedId, setExpandedId] = useState<string | null>(null);

    const filters: ActionItemFilters = useMemo(() => {
        const f: ActionItemFilters = { status: statusForTab(tab), limit: 100 };
        if (assignee) f.assigned_to = assignee;
        if (q) f.q = q;
        return f;
    }, [tab, assignee, q]);

    const { data: items, isLoading, error } = useActionItems(filters);
    const usersQuery = useTenantUsers();
    const update = useUpdateActionItem();

    const usersById = useMemo(() => {
        const map = new Map<string, string>();
        for (const u of usersQuery.data ?? []) {
            map.set(u.id, u.name || u.email);
        }
        return map;
    }, [usersQuery.data]);

    const filtered = useMemo(() => {
        if (!items) return [];
        return items.filter((it) => {
            if (dueFrom && (!it.due_date || it.due_date < dueFrom)) return false;
            if (dueTo && (!it.due_date || it.due_date > dueTo)) return false;
            return true;
        });
    }, [items, dueFrom, dueTo]);

    return (
        <div className="space-y-6">
            <header className="flex items-start justify-between gap-4 flex-wrap">
                <div>
                    <h2 className="text-2xl font-bold">Action items</h2>
                    <p className="text-text-muted mt-1">
                        Everything Linda pulled out of your calls — assign, snooze, or close.
                    </p>
                </div>
                <div className="inline-flex rounded-lg border border-border bg-bg-card p-1">
                    {TABS.map((t) => (
                        <button
                            key={t.key}
                            onClick={() => setTab(t.key)}
                            className={`px-3 py-1.5 text-sm rounded-md transition ${
                                tab === t.key
                                    ? "bg-primary text-white"
                                    : "text-text-muted hover:text-text-main"
                            }`}
                        >
                            {t.label}
                        </button>
                    ))}
                </div>
            </header>

            <section className="rounded-lg border border-border bg-bg-card p-4">
                <div className="grid grid-cols-1 gap-3 md:grid-cols-4">
                    <label className="flex flex-col gap-1 text-sm">
                        <span className="text-xs uppercase tracking-wide text-text-subtle">
                            Search
                        </span>
                        <input
                            type="text"
                            value={q}
                            onChange={(e) => setQ(e.target.value)}
                            placeholder="Title or description…"
                            className="rounded-md border border-border bg-bg-elevated px-3 py-1.5 text-sm"
                        />
                    </label>
                    <label className="flex flex-col gap-1 text-sm">
                        <span className="text-xs uppercase tracking-wide text-text-subtle">
                            Assignee
                        </span>
                        {usersQuery.data ? (
                            <select
                                value={assignee}
                                onChange={(e) => setAssignee(e.target.value)}
                                className="rounded-md border border-border bg-bg-elevated px-3 py-1.5 text-sm"
                            >
                                <option value="">Anyone</option>
                                {usersQuery.data.map((u) => (
                                    <option key={u.id} value={u.id}>
                                        {u.name || u.email}
                                    </option>
                                ))}
                            </select>
                        ) : (
                            <input
                                type="text"
                                value={assignee}
                                onChange={(e) => setAssignee(e.target.value)}
                                placeholder="User ID"
                                className="rounded-md border border-border bg-bg-elevated px-3 py-1.5 text-sm"
                            />
                        )}
                    </label>
                    <label className="flex flex-col gap-1 text-sm">
                        <span className="text-xs uppercase tracking-wide text-text-subtle">
                            Due from
                        </span>
                        <input
                            type="date"
                            value={dueFrom}
                            onChange={(e) => setDueFrom(e.target.value)}
                            className="rounded-md border border-border bg-bg-elevated px-3 py-1.5 text-sm"
                        />
                    </label>
                    <label className="flex flex-col gap-1 text-sm">
                        <span className="text-xs uppercase tracking-wide text-text-subtle">
                            Due to
                        </span>
                        <input
                            type="date"
                            value={dueTo}
                            onChange={(e) => setDueTo(e.target.value)}
                            className="rounded-md border border-border bg-bg-elevated px-3 py-1.5 text-sm"
                        />
                    </label>
                </div>
            </section>

            {error ? (
                <div className="rounded-lg border border-accent-rose bg-bg-card p-4 text-sm text-accent-rose">
                    Couldn&apos;t load action items: {(error as Error).message}
                </div>
            ) : isLoading ? (
                <SkeletonTable />
            ) : filtered.length === 0 ? (
                <EmptyState />
            ) : (
                <section className="rounded-lg border border-border bg-bg-card overflow-hidden">
                    <table className="w-full text-sm">
                        <thead className="bg-bg-secondary text-text-subtle text-xs uppercase tracking-wide">
                            <tr>
                                <th className="px-4 py-2 text-left">Title</th>
                                <th className="px-4 py-2 text-left">Customer</th>
                                <th className="px-4 py-2 text-left">Assignee</th>
                                <th className="px-4 py-2 text-left">Due</th>
                                <th className="px-4 py-2 text-left">Status</th>
                                <th className="px-4 py-2 text-left">Last update</th>
                                <th className="px-4 py-2 text-right">Actions</th>
                            </tr>
                        </thead>
                        <tbody>
                            {filtered.map((item) => (
                                <Row
                                    key={item.id}
                                    item={item}
                                    expanded={expandedId === item.id}
                                    onToggle={() =>
                                        setExpandedId((cur) =>
                                            cur === item.id ? null : item.id,
                                        )
                                    }
                                    assigneeLabel={
                                        item.assigned_to
                                            ? usersById.get(item.assigned_to) ??
                                              item.assigned_to
                                            : "—"
                                    }
                                    onMarkDone={() =>
                                        update.mutate({
                                            id: item.id,
                                            patch: { status: "done" },
                                        })
                                    }
                                    onSnooze={() =>
                                        update.mutate({
                                            id: item.id,
                                            patch: {
                                                status: "snoozed",
                                                due_date: inOneDayISODate(),
                                            },
                                        })
                                    }
                                    onReopen={() =>
                                        update.mutate({
                                            id: item.id,
                                            patch: { status: "pending" },
                                        })
                                    }
                                    pending={update.isPending}
                                />
                            ))}
                        </tbody>
                    </table>
                </section>
            )}

            {update.isError ? (
                <div className="rounded-lg border border-accent-rose bg-bg-card p-3 text-sm text-accent-rose">
                    Update failed: {(update.error as Error).message}
                </div>
            ) : null}
        </div>
    );
}

function Row({
    item,
    expanded,
    onToggle,
    onMarkDone,
    onSnooze,
    onReopen,
    pending,
    assigneeLabel,
}: {
    item: ActionItem;
    expanded: boolean;
    onToggle: () => void;
    onMarkDone: () => void;
    onSnooze: () => void;
    onReopen: () => void;
    pending: boolean;
    assigneeLabel: string;
}) {
    const isDone = DONE_STATUSES.has(item.status);
    const isSnoozed = item.status === "snoozed";
    return (
        <>
            <tr
                onClick={onToggle}
                className="cursor-pointer border-t border-border hover:bg-bg-secondary"
            >
                <td className="px-4 py-3 font-medium">{item.title}</td>
                <td className="px-4 py-3 text-text-muted">
                    {item.category ?? "—"}
                </td>
                <td className="px-4 py-3 text-text-muted">{assigneeLabel}</td>
                <td className="px-4 py-3 text-text-muted">
                    {fmtDate(item.due_date)}
                </td>
                <td className="px-4 py-3">
                    <StatusPill status={item.status} />
                </td>
                <td className="px-4 py-3 text-text-muted">
                    {fmtDate(item.created_at)}
                </td>
                <td
                    className="px-4 py-3 text-right"
                    onClick={(e) => e.stopPropagation()}
                >
                    <div className="inline-flex gap-2">
                        {!isDone ? (
                            <button
                                onClick={onMarkDone}
                                disabled={pending}
                                className="rounded-md border border-border px-2 py-1 text-xs hover:bg-bg-secondary disabled:opacity-50"
                            >
                                Mark done
                            </button>
                        ) : null}
                        {!isSnoozed && !isDone ? (
                            <button
                                onClick={onSnooze}
                                disabled={pending}
                                className="rounded-md border border-border px-2 py-1 text-xs hover:bg-bg-secondary disabled:opacity-50"
                            >
                                Snooze 1d
                            </button>
                        ) : null}
                        {(isDone || isSnoozed) ? (
                            <button
                                onClick={onReopen}
                                disabled={pending}
                                className="rounded-md border border-border px-2 py-1 text-xs hover:bg-bg-secondary disabled:opacity-50"
                            >
                                Reopen
                            </button>
                        ) : null}
                    </div>
                </td>
            </tr>
            {expanded ? (
                <tr className="border-t border-border bg-bg-secondary">
                    <td colSpan={7} className="px-4 py-3 text-sm">
                        <div className="space-y-2">
                            {item.description ? (
                                <p className="text-text-muted whitespace-pre-wrap">
                                    {item.description}
                                </p>
                            ) : (
                                <p className="text-text-subtle italic">
                                    No description on this action item.
                                </p>
                            )}
                            <div className="flex gap-4 text-xs text-text-subtle">
                                <span>Priority: {item.priority}</span>
                                <span>Automation: {item.automation_status}</span>
                            </div>
                            <Link
                                href={`/interactions/${item.interaction_id}`}
                                className="inline-flex items-center gap-1 text-primary text-sm hover:underline"
                            >
                                Open source interaction →
                            </Link>
                        </div>
                    </td>
                </tr>
            ) : null}
        </>
    );
}

function StatusPill({ status }: { status: string }) {
    const tone = DONE_STATUSES.has(status)
        ? "text-accent-emerald"
        : status === "snoozed"
          ? "text-accent-amber"
          : status === "dismissed" || status === "rejected"
            ? "text-text-subtle"
            : "text-primary";
    return (
        <span className={`text-xs font-medium capitalize ${tone}`}>
            {status}
        </span>
    );
}

function SkeletonTable() {
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
        <div className="rounded-lg border border-border border-dashed bg-bg-card p-10 text-center">
            <p className="text-text-muted">
                Linda will surface action items here as you process calls.
            </p>
            <p className="text-text-subtle text-sm mt-1">
                Upload a call from{" "}
                <Link href="/interactions" className="text-primary hover:underline">
                    /interactions
                </Link>{" "}
                to get started.
            </p>
        </div>
    );
}
