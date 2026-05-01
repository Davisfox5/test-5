"use client";

import { useMemo, useState } from "react";
import { AuditLogRow, useAuditLogs } from "@/lib/audit-log";
import { humanizeError } from "@/components/admin/section";

/**
 * Settings → Audit log.
 *
 * Admin-only — gated at the page level via ``AdminGate``. Renders a
 * filter bar + paginated table of every mutating operation against the
 * tenant. Click a row to expand its before/after diff.
 *
 * Role rule: admin-only on the SPA. The backend ``/admin/*`` routes are
 * already gated by ``require_role("admin")`` so a manager who somehow
 * landed on this section would get a 403 from the API anyway.
 */
export function AuditLogSection() {
    const [action, setAction] = useState("");
    const [resourceType, setResourceType] = useState("");
    const [actor, setActor] = useState("");
    const [from, setFrom] = useState("");
    const [to, setTo] = useState("");
    const [offset, setOffset] = useState(0);
    const [expanded, setExpanded] = useState<string | null>(null);

    const filters = useMemo(
        () => ({
            action: action || undefined,
            resource_type: resourceType || undefined,
            actor: actor || undefined,
            from: from || undefined,
            to: to || undefined,
            limit: 50,
            offset,
        }),
        [action, resourceType, actor, from, to, offset],
    );

    const { data, isLoading, error } = useAuditLogs(filters);

    const onApply = () => setOffset(0);

    return (
        <div className="space-y-3">
            <div className="grid grid-cols-1 gap-2 md:grid-cols-5">
                <input
                    type="text"
                    placeholder="action (e.g. webhook.deleted)"
                    className="rounded-md border border-border bg-bg-raised px-3 py-2 text-xs"
                    value={action}
                    onChange={(e) => setAction(e.target.value)}
                />
                <input
                    type="text"
                    placeholder="resource_type (e.g. user)"
                    className="rounded-md border border-border bg-bg-raised px-3 py-2 text-xs"
                    value={resourceType}
                    onChange={(e) => setResourceType(e.target.value)}
                />
                <input
                    type="text"
                    placeholder="actor (uuid | api_key | system)"
                    className="rounded-md border border-border bg-bg-raised px-3 py-2 text-xs"
                    value={actor}
                    onChange={(e) => setActor(e.target.value)}
                />
                <input
                    type="datetime-local"
                    className="rounded-md border border-border bg-bg-raised px-3 py-2 text-xs"
                    value={from}
                    onChange={(e) => setFrom(e.target.value)}
                />
                <input
                    type="datetime-local"
                    className="rounded-md border border-border bg-bg-raised px-3 py-2 text-xs"
                    value={to}
                    onChange={(e) => setTo(e.target.value)}
                />
            </div>
            <div className="flex justify-end">
                <button
                    type="button"
                    onClick={onApply}
                    className="rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-white"
                >
                    Apply filters
                </button>
            </div>

            {isLoading ? (
                <p className="text-sm text-text-muted">Loading audit log…</p>
            ) : error ? (
                <p className="text-sm text-accent-rose">
                    {humanizeError(error)}
                </p>
            ) : !data?.items?.length ? (
                <p className="text-sm text-text-muted">
                    No audit rows match the current filters.
                </p>
            ) : (
                <>
                    <table className="w-full text-xs">
                        <thead>
                            <tr className="text-left uppercase tracking-wide text-text-subtle">
                                <th className="pb-2">Time</th>
                                <th className="pb-2">Actor</th>
                                <th className="pb-2">Action</th>
                                <th className="pb-2">Resource</th>
                                <th className="pb-2 sr-only">Diff</th>
                            </tr>
                        </thead>
                        <tbody>
                            {data.items.map((row) => (
                                <RowOrDetail
                                    key={row.id}
                                    row={row}
                                    expanded={expanded === row.id}
                                    onToggle={() =>
                                        setExpanded(
                                            expanded === row.id
                                                ? null
                                                : row.id,
                                        )
                                    }
                                />
                            ))}
                        </tbody>
                    </table>

                    <div className="flex items-center justify-between text-xs text-text-subtle">
                        <span>
                            Showing {data.offset + 1}–
                            {data.offset + data.items.length} of {data.total}
                        </span>
                        <div className="space-x-2">
                            <button
                                type="button"
                                disabled={data.offset === 0}
                                onClick={() =>
                                    setOffset(Math.max(0, offset - 50))
                                }
                                className="rounded-md border border-border px-2 py-1 disabled:opacity-50"
                            >
                                Prev
                            </button>
                            <button
                                type="button"
                                disabled={
                                    data.offset + data.items.length >=
                                    data.total
                                }
                                onClick={() => setOffset(offset + 50)}
                                className="rounded-md border border-border px-2 py-1 disabled:opacity-50"
                            >
                                Next
                            </button>
                        </div>
                    </div>
                </>
            )}
        </div>
    );
}

function RowOrDetail({
    row,
    expanded,
    onToggle,
}: {
    row: AuditLogRow;
    expanded: boolean;
    onToggle: () => void;
}) {
    return (
        <>
            <tr
                className="cursor-pointer border-t border-border align-middle hover:bg-bg-raised"
                onClick={onToggle}
            >
                <td className="py-1 text-text-subtle">
                    {new Date(row.created_at).toLocaleString()}
                </td>
                <td className="py-1">
                    {row.actor_principal === "user"
                        ? row.actor_user_id?.slice(0, 8) ?? "user"
                        : row.actor_principal}
                </td>
                <td className="py-1 font-mono">{row.action}</td>
                <td className="py-1 font-mono text-text-subtle">
                    {row.resource_type}
                    {row.resource_id ? `/${row.resource_id.slice(0, 8)}` : ""}
                </td>
                <td className="py-1 text-right text-primary">
                    {expanded ? "▾" : "▸"}
                </td>
            </tr>
            {expanded ? (
                <tr className="border-t border-border bg-bg-raised">
                    <td colSpan={5} className="px-3 py-2">
                        <DiffPanel row={row} />
                    </td>
                </tr>
            ) : null}
        </>
    );
}

function DiffPanel({ row }: { row: AuditLogRow }) {
    return (
        <div className="space-y-2 text-xs">
            <div className="grid grid-cols-2 gap-2">
                <div>
                    <p className="font-medium text-text-subtle">Before</p>
                    <pre className="mt-1 max-h-48 overflow-auto rounded-md border border-border bg-bg p-2 font-mono">
                        {row.before
                            ? JSON.stringify(row.before, null, 2)
                            : "—"}
                    </pre>
                </div>
                <div>
                    <p className="font-medium text-text-subtle">After</p>
                    <pre className="mt-1 max-h-48 overflow-auto rounded-md border border-border bg-bg p-2 font-mono">
                        {row.after ? JSON.stringify(row.after, null, 2) : "—"}
                    </pre>
                </div>
            </div>
            {Object.keys(row.meta || {}).length > 0 ? (
                <details className="text-text-subtle">
                    <summary className="cursor-pointer">Metadata</summary>
                    <pre className="mt-1 max-h-32 overflow-auto rounded-md border border-border bg-bg p-2 font-mono">
                        {JSON.stringify(row.meta, null, 2)}
                    </pre>
                </details>
            ) : null}
        </div>
    );
}
