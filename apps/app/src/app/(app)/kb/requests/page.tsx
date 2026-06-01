"use client";

/**
 * KB-edit-request inbox.
 *
 * The destination for the ``update_kb_article`` and
 * ``escalate_recurring_issue`` Apply paths on the manager portal.
 * Each row is a request the KB owner triages: change status, assign,
 * dismiss, or mark published.
 */

import { useState } from "react";
import { useMe } from "@/lib/me";
import {
    PRIORITY_LABEL,
    STATUS_LABEL,
    useKBRequests,
    usePatchKBRequest,
    type KBRequest,
    type KBRequestStatus,
} from "@/lib/kb_requests";

const TABS: { key: KBRequestStatus | "all"; label: string }[] = [
    { key: "open", label: "Open" },
    { key: "in_progress", label: "In progress" },
    { key: "published", label: "Published" },
    { key: "dismissed", label: "Dismissed" },
];

export default function KBRequestsPage() {
    const me = useMe();
    const managerDomains = me.data?.user?.manager_domains || [];
    const agentDomains = me.data?.user?.agent_domains || [];
    const isTenantAdmin = me.data?.user?.is_tenant_admin ?? false;
    const allowed =
        isTenantAdmin ||
        agentDomains.includes("it_support") ||
        managerDomains.includes("it_support") ||
        managerDomains.includes("customer_service");

    const [tab, setTab] = useState<KBRequestStatus | "all">("open");
    const { data: rows = [], isLoading } = useKBRequests(
        tab === "all" ? {} : { status: tab },
    );
    const patch = usePatchKBRequest();

    if (me.isLoading) return <p className="text-text-muted">Loading…</p>;
    if (!me.data?.user)
        return <p className="text-text-muted">Sign in to view KB requests.</p>;
    if (!allowed) {
        return (
            <div className="rounded-lg border border-border bg-bg-card p-4">
                <p className="text-text">You don't have KB request access.</p>
                <p className="mt-1 text-sm text-text-muted">
                    Ask your tenant admin to add IT Support or Customer Success
                    manager scope under Settings &rarr; User Management.
                </p>
            </div>
        );
    }

    return (
        <div className="space-y-6">
            <header className="flex items-baseline justify-between gap-4">
                <h1 className="text-2xl font-bold">KB requests</h1>
                <p className="text-sm text-text-muted">
                    Edits and new articles requested by the team.
                </p>
            </header>

            <div className="flex flex-wrap items-center gap-1 border-b border-border">
                {TABS.map((t) => (
                    <button
                        key={t.key}
                        onClick={() => setTab(t.key)}
                        className={
                            "rounded-t-md px-3 py-1.5 text-sm font-medium transition " +
                            (tab === t.key
                                ? "border-b-2 border-primary text-text"
                                : "text-text-muted hover:text-text")
                        }
                    >
                        {t.label}
                    </button>
                ))}
            </div>

            <div className="rounded-lg border border-border bg-bg-card">
                {isLoading ? (
                    <p className="p-4 text-sm text-text-muted">Loading…</p>
                ) : rows.length === 0 ? (
                    <p className="p-4 text-sm text-text-muted">
                        No requests in this view.
                    </p>
                ) : (
                    <ul className="divide-y divide-border">
                        {rows.map((r) => (
                            <KBRow
                                key={r.id}
                                r={r}
                                onPatch={(next) =>
                                    patch.mutate({ id: r.id, patch: next })
                                }
                            />
                        ))}
                    </ul>
                )}
            </div>
        </div>
    );
}

function KBRow({
    r,
    onPatch,
}: {
    r: KBRequest;
    onPatch: (p: Partial<Pick<KBRequest, "status" | "priority">>) => void;
}) {
    return (
        <li className="p-3">
            <div className="flex items-start justify-between gap-4">
                <div className="space-y-1">
                    <p className="text-sm font-medium text-text">{r.topic}</p>
                    {r.rationale && (
                        <p className="text-xs text-text-muted">{r.rationale}</p>
                    )}
                    <p className="text-xs text-text-subtle">
                        Filed {new Date(r.created_at).toLocaleDateString()}
                        {r.assigned_to_name
                            ? ` · Assigned to ${r.assigned_to_name}`
                            : ""}
                        {r.source_recommendation_id
                            ? " · From a manager recommendation"
                            : ""}
                    </p>
                </div>
                <div className="flex shrink-0 flex-col gap-2">
                    <select
                        value={r.status}
                        onChange={(e) =>
                            onPatch({
                                status: e.target.value as KBRequest["status"],
                            })
                        }
                        className="rounded border border-border bg-bg-card px-2 py-1 text-xs"
                    >
                        {Object.entries(STATUS_LABEL).map(([v, label]) => (
                            <option key={v} value={v}>
                                {label}
                            </option>
                        ))}
                    </select>
                    <select
                        value={r.priority}
                        onChange={(e) =>
                            onPatch({
                                priority: e.target.value as KBRequest["priority"],
                            })
                        }
                        className="rounded border border-border bg-bg-card px-2 py-1 text-xs"
                    >
                        {Object.entries(PRIORITY_LABEL).map(([v, label]) => (
                            <option key={v} value={v}>
                                {label}
                            </option>
                        ))}
                    </select>
                </div>
            </div>
        </li>
    );
}
