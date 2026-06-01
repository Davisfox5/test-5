"use client";

/**
 * Support case detail page.
 *
 * Renders one case's lifecycle, the linked-interaction timeline,
 * assignment + priority + status controls, and a CSAT capture row
 * (internal + public-link issuance). Mounted at /support/cases/[id].
 */

import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";
import {
    useAssignCase,
    useIssueCsatToken,
    useRecordCsat,
    useSetPriority,
    useSupportCase,
    useTransitionCase,
    PRIORITY_LABEL,
    STATUS_COLORS,
    STATUS_LABEL,
    type CasePriority,
    type CaseStatus,
} from "@/lib/support";
import { useUsers } from "@/lib/users";

const STATUS_OPTIONS: CaseStatus[] = [
    "open",
    "in_progress",
    "escalated",
    "resolved",
    "closed",
];
const PRIORITY_OPTIONS: CasePriority[] = ["high", "medium", "low"];

export default function CaseDetailPage() {
    const params = useParams();
    const caseId =
        typeof params.id === "string" ? params.id : params.id?.[0] || null;
    const { data: c, isLoading } = useSupportCase(caseId);
    const transition = useTransitionCase();
    const assign = useAssignCase();
    const setPriority = useSetPriority();
    const recordCsat = useRecordCsat();
    const issueToken = useIssueCsatToken();
    const { data: users = [] } = useUsers(false);
    const [tokenLink, setTokenLink] = useState<string | null>(null);

    if (isLoading || !caseId) {
        return <p className="text-text-muted">Loading…</p>;
    }
    if (!c) {
        return (
            <div className="rounded-lg border border-border bg-bg-card p-4">
                <p className="text-text">Case not found.</p>
                <Link href="/support" className="mt-2 inline-block text-sm underline">
                    Back to queue
                </Link>
            </div>
        );
    }

    return (
        <div className="space-y-6">
            <header className="space-y-1">
                <Link
                    href="/support"
                    className="text-xs text-text-muted hover:underline"
                >
                    ← Back to queue
                </Link>
                <div className="flex flex-wrap items-baseline gap-3">
                    <h1 className="text-2xl font-bold">{c.subject}</h1>
                    <span
                        className={`rounded border px-2 py-0.5 text-[10px] font-semibold uppercase ${STATUS_COLORS[c.status]}`}
                    >
                        {STATUS_LABEL[c.status]}
                    </span>
                </div>
                {c.customer_name && (
                    <p className="text-sm text-text-muted">
                        Customer: <strong>{c.customer_name}</strong>
                    </p>
                )}
            </header>

            <section className="grid gap-4 md:grid-cols-2">
                <div className="rounded-lg border border-border bg-bg-card p-4">
                    <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-text-muted">
                        Lifecycle
                    </h2>
                    <dl className="grid grid-cols-2 gap-y-1 text-sm">
                        <dt className="text-text-muted">Opened</dt>
                        <dd>{new Date(c.opened_at).toLocaleString()}</dd>
                        <dt className="text-text-muted">First response</dt>
                        <dd>
                            {c.first_response_at
                                ? new Date(c.first_response_at).toLocaleString()
                                : "—"}
                        </dd>
                        <dt className="text-text-muted">Escalated</dt>
                        <dd>
                            {c.escalated_at
                                ? new Date(c.escalated_at).toLocaleString()
                                : "—"}
                        </dd>
                        <dt className="text-text-muted">Resolved</dt>
                        <dd>
                            {c.resolved_at
                                ? new Date(c.resolved_at).toLocaleString()
                                : "—"}
                        </dd>
                        <dt className="text-text-muted">Closed</dt>
                        <dd>
                            {c.closed_at
                                ? new Date(c.closed_at).toLocaleString()
                                : "—"}
                        </dd>
                        {c.first_contact_resolution !== null && (
                            <>
                                <dt className="text-text-muted">FCR</dt>
                                <dd>
                                    {c.first_contact_resolution ? "Yes" : "No"}
                                </dd>
                            </>
                        )}
                    </dl>
                </div>

                <div className="rounded-lg border border-border bg-bg-card p-4 space-y-3">
                    <h2 className="text-sm font-semibold uppercase tracking-wide text-text-muted">
                        Controls
                    </h2>
                    <label className="block text-xs">
                        Status
                        <select
                            value={c.status}
                            onChange={(e) =>
                                transition.mutate({
                                    caseId,
                                    status: e.target.value as CaseStatus,
                                })
                            }
                            className="mt-1 w-full rounded border border-border bg-bg-card px-2 py-1 text-sm"
                        >
                            {STATUS_OPTIONS.map((s) => (
                                <option key={s} value={s}>
                                    {STATUS_LABEL[s]}
                                </option>
                            ))}
                        </select>
                    </label>
                    <label className="block text-xs">
                        Priority
                        <select
                            value={c.priority}
                            onChange={(e) =>
                                setPriority.mutate({
                                    caseId,
                                    priority: e.target.value as CasePriority,
                                })
                            }
                            className="mt-1 w-full rounded border border-border bg-bg-card px-2 py-1 text-sm"
                        >
                            {PRIORITY_OPTIONS.map((p) => (
                                <option key={p} value={p}>
                                    {PRIORITY_LABEL[p]}
                                </option>
                            ))}
                        </select>
                    </label>
                    <label className="block text-xs">
                        Assignee
                        <select
                            value={c.assigned_to ?? ""}
                            onChange={(e) =>
                                assign.mutate({
                                    caseId,
                                    userId: e.target.value || null,
                                })
                            }
                            className="mt-1 w-full rounded border border-border bg-bg-card px-2 py-1 text-sm"
                        >
                            <option value="">Unassigned</option>
                            {users
                                .filter(
                                    (u) =>
                                        u.is_active &&
                                        (u.is_tenant_admin ||
                                            u.agent_domains.includes("it_support") ||
                                            u.manager_domains.includes("it_support")),
                                )
                                .map((u) => (
                                    <option key={u.id} value={u.id}>
                                        {u.name || u.email}
                                    </option>
                                ))}
                        </select>
                    </label>
                </div>
            </section>

            <section className="rounded-lg border border-border bg-bg-card p-4">
                <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-text-muted">
                    Customer experience (CSAT)
                </h2>
                {c.csat_score !== null ? (
                    <p className="text-sm">
                        Recorded score:{" "}
                        <strong className="text-text">{c.csat_score} / 5</strong>
                    </p>
                ) : c.status !== "resolved" && c.status !== "closed" ? (
                    <p className="text-sm text-text-muted">
                        Available once the case is resolved or closed.
                    </p>
                ) : (
                    <div className="space-y-3">
                        <p className="text-sm text-text-muted">
                            Capture from a phone follow-up or share a public
                            link the customer fills in themselves.
                        </p>
                        <div className="flex flex-wrap items-center gap-3">
                            <div className="flex gap-1">
                                {[1, 2, 3, 4, 5].map((n) => (
                                    <button
                                        key={n}
                                        onClick={() =>
                                            recordCsat.mutate({ caseId, score: n })
                                        }
                                        disabled={recordCsat.isPending}
                                        className="rounded border border-border bg-bg px-3 py-1 text-sm hover:bg-bg-card-hover"
                                        title={`Record ${n}/5`}
                                    >
                                        {n}
                                    </button>
                                ))}
                            </div>
                            <button
                                onClick={async () => {
                                    const res = await issueToken.mutateAsync(caseId);
                                    setTokenLink(res.public_url);
                                }}
                                disabled={issueToken.isPending}
                                className="rounded border border-border bg-bg-card px-3 py-1 text-sm hover:bg-bg-card-hover"
                            >
                                {issueToken.isPending
                                    ? "Generating…"
                                    : "Generate public link"}
                            </button>
                        </div>
                        {tokenLink && (
                            <p className="text-xs text-text-muted break-all">
                                Share with the customer:{" "}
                                <code className="rounded bg-bg px-1 py-0.5 font-mono text-[11px]">
                                    {tokenLink}
                                </code>
                            </p>
                        )}
                    </div>
                )}
            </section>

            <section>
                <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-text-muted">
                    Interactions ({c.interactions.length})
                </h2>
                <div className="rounded-lg border border-border bg-bg-card">
                    {c.interactions.length === 0 ? (
                        <p className="p-4 text-sm text-text-muted">
                            No interactions linked yet.
                        </p>
                    ) : (
                        <ul className="divide-y divide-border">
                            {c.interactions.map((ix) => (
                                <li key={ix.id} className="p-3">
                                    <div className="flex items-center justify-between gap-2">
                                        <Link
                                            href={`/interactions/${ix.id}`}
                                            className="text-sm font-medium text-text hover:underline"
                                        >
                                            {ix.title || `Untitled ${ix.channel}`}
                                        </Link>
                                        <span className="text-xs text-text-subtle">
                                            {new Date(
                                                ix.created_at,
                                            ).toLocaleString()}
                                        </span>
                                    </div>
                                    <div className="mt-1 text-xs text-text-muted">
                                        {ix.channel}
                                        {ix.direction ? ` · ${ix.direction}` : ""}
                                    </div>
                                </li>
                            ))}
                        </ul>
                    )}
                </div>
            </section>
        </div>
    );
}
