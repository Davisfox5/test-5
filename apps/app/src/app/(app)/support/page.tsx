"use client";

/**
 * IT Support agent portal — case queue.
 *
 * Replaces the scaffold from PR #113. Renders a filterable list of
 * cases (open_all / mine / status / priority) with quick triage from
 * the row (status change, priority change). Detail view lives at
 * /support/cases/[id].
 */

import Link from "next/link";
import { useMemo, useState } from "react";
import { useMe } from "@/lib/me";
import { useManagerAlerts, type ManagerAlert } from "@/lib/manager";
import {
    useSupportCases,
    type CasePriority,
    type CaseStatus,
    type SupportCaseSummary,
    STATUS_COLORS,
    STATUS_LABEL,
    PRIORITY_LABEL,
} from "@/lib/support";

const FILTER_TABS: { key: "open_all" | "mine" | CaseStatus; label: string }[] = [
    { key: "open_all", label: "Open" },
    { key: "mine", label: "Mine" },
    { key: "escalated", label: "Escalated" },
    { key: "resolved", label: "Resolved" },
    { key: "closed", label: "Closed" },
];

export default function SupportPortalPage() {
    const me = useMe();
    const agentDomains = me.data?.user?.agent_domains || [];
    const managerDomains = me.data?.user?.manager_domains || [];
    const isTenantAdmin = me.data?.user?.is_tenant_admin ?? false;
    const allowed =
        isTenantAdmin ||
        agentDomains.includes("it_support") ||
        managerDomains.includes("it_support");

    const [tab, setTab] =
        useState<"open_all" | "mine" | CaseStatus>("open_all");
    const [priority, setPriority] = useState<CasePriority | "">("");

    const params = useMemo(() => {
        const p: {
            status?: CaseStatus | "open_all";
            mineOnly?: boolean;
            priority?: CasePriority;
        } = {};
        if (tab === "mine") p.mineOnly = true;
        else if (tab !== "open_all") p.status = tab;
        else p.status = "open_all";
        if (priority) p.priority = priority;
        return p;
    }, [tab, priority]);

    const { data: cases = [], isLoading } = useSupportCases(params);

    if (me.isLoading) return <p className="text-text-muted">Loading…</p>;
    if (!me.data?.user)
        return (
            <p className="text-text-muted">Sign in to view the support portal.</p>
        );
    if (!allowed) {
        return (
            <div className="rounded-lg border border-border bg-bg-card p-4">
                <p className="text-text">You don't have IT Support access.</p>
                <p className="mt-1 text-sm text-text-muted">
                    Ask your tenant admin to add IT Support to your agent or
                    manager motions under Settings &rarr; User Management.
                </p>
            </div>
        );
    }

    return (
        <div className="space-y-6">
            <header className="flex items-baseline justify-between gap-4">
                <h1 className="text-2xl font-bold">IT Support</h1>
                <p className="text-sm text-text-muted">
                    Cases, escalations, and CSAT for your queue.
                </p>
            </header>

            <SupportAlertsStrip />

            <section>
                <div className="flex flex-wrap items-center justify-between gap-2">
                    <div className="flex flex-wrap items-center gap-1">
                        {FILTER_TABS.map((t) => (
                            <button
                                key={t.key}
                                onClick={() => setTab(t.key)}
                                className={
                                    "rounded-t-md border-b-2 px-3 py-1.5 text-sm font-medium transition " +
                                    (tab === t.key
                                        ? "border-primary text-text"
                                        : "border-transparent text-text-muted hover:text-text")
                                }
                            >
                                {t.label}
                            </button>
                        ))}
                    </div>
                    <label className="text-xs text-text-muted">
                        Priority:{" "}
                        <select
                            value={priority}
                            onChange={(e) =>
                                setPriority(e.target.value as CasePriority | "")
                            }
                            className="ml-1 rounded border border-border bg-bg-card px-2 py-1 text-xs text-text"
                        >
                            <option value="">All</option>
                            <option value="high">High</option>
                            <option value="medium">Medium</option>
                            <option value="low">Low</option>
                        </select>
                    </label>
                </div>

                <div className="mt-2 rounded-lg border border-border bg-bg-card">
                    {isLoading ? (
                        <p className="p-4 text-sm text-text-muted">Loading…</p>
                    ) : cases.length === 0 ? (
                        <p className="p-4 text-sm text-text-muted">
                            No cases match this filter.
                        </p>
                    ) : (
                        <table className="w-full text-sm">
                            <thead>
                                <tr className="text-left text-xs uppercase tracking-wide text-text-subtle">
                                    <th className="px-3 py-2">Subject</th>
                                    <th className="px-3 py-2">Account</th>
                                    <th className="px-3 py-2">Status</th>
                                    <th className="px-3 py-2">Priority</th>
                                    <th className="px-3 py-2">Assignee</th>
                                    <th className="px-3 py-2 text-right">Opened</th>
                                    <th className="px-3 py-2 text-right">Touches</th>
                                </tr>
                            </thead>
                            <tbody>
                                {cases.map((c) => (
                                    <CaseRow key={c.id} c={c} />
                                ))}
                            </tbody>
                        </table>
                    )}
                </div>
            </section>
        </div>
    );
}

function CaseRow({ c }: { c: SupportCaseSummary }) {
    return (
        <tr className="border-t border-border hover:bg-bg-card-hover">
            <td className="px-3 py-2">
                <Link
                    href={`/support/cases/${c.id}`}
                    className="text-sm font-medium text-text hover:underline"
                >
                    {c.subject}
                </Link>
            </td>
            <td className="px-3 py-2 text-sm text-text-muted">
                {c.customer_name || "—"}
            </td>
            <td className="px-3 py-2">
                <span
                    className={`rounded border px-2 py-0.5 text-[10px] font-semibold uppercase ${STATUS_COLORS[c.status]}`}
                >
                    {STATUS_LABEL[c.status]}
                </span>
            </td>
            <td className="px-3 py-2 text-sm">{PRIORITY_LABEL[c.priority]}</td>
            <td className="px-3 py-2 text-sm text-text-muted">
                {c.assigned_to_name || "Unassigned"}
            </td>
            <td className="px-3 py-2 text-right text-xs text-text-subtle">
                {new Date(c.opened_at).toLocaleDateString()}
            </td>
            <td className="px-3 py-2 text-right text-sm">{c.interaction_count}</td>
        </tr>
    );
}

function SupportAlertsStrip() {
    const { data: alerts = [] } = useManagerAlerts({
        onlyOpen: true,
        domain: "it_support",
    });
    if (alerts.length === 0) return null;
    return (
        <section>
            <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-text-muted">
                Manager alerts on your motion ({alerts.length})
            </h2>
            <div className="grid gap-2 md:grid-cols-2">
                {alerts.slice(0, 4).map((a) => (
                    <SupportAlertPreview key={a.id} alert={a} />
                ))}
            </div>
        </section>
    );
}

function SupportAlertPreview({ alert }: { alert: ManagerAlert }) {
    return (
        <div className="rounded-lg border border-border bg-bg-card p-3">
            <p className="text-sm font-medium text-text">{alert.title}</p>
            {alert.body && <p className="mt-1 text-xs text-text-muted">{alert.body}</p>}
        </div>
    );
}
