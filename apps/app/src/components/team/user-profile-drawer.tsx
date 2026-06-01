"use client";

/**
 * Admin user-profile drawer.
 *
 * Opened from the /team page when an admin clicks a user. Renders the
 * user's full record, lets the admin edit name + motion scopes + role,
 * reset their password, and view the per-user audit log (every
 * mutation that touched this row + every action this user took).
 *
 * Suspend / reactivate stays on the parent table — keeping it where
 * the seat-reconciliation banner already lives means the bulk path
 * (admin reactivating someone after a tier upgrade) doesn't require
 * opening the drawer.
 */

import { useEffect, useState } from "react";
import type { Domain } from "@/lib/me";
import {
    TeamUser,
    UserPatchPayload,
    useSetUserPassword,
    useUserAuditLog,
    type AuditLogRow,
} from "@/lib/users";
import { DOMAIN_LABEL } from "@/lib/manager";

interface Props {
    user: TeamUser | null;
    onClose: () => void;
    onPatch: (id: string, patch: UserPatchPayload) => void;
    canEdit: boolean;
}

const GRID_DOMAINS: Domain[] = ["sales", "customer_service", "it_support"];
const ROLES = ["agent", "manager", "admin"] as const;

export function UserProfileDrawer({ user, onClose, onPatch, canEdit }: Props) {
    if (!user) return null;
    return (
        <div
            className="fixed inset-0 z-40 flex justify-end"
            role="dialog"
            aria-modal="true"
        >
            <button
                type="button"
                aria-label="Close"
                onClick={onClose}
                className="flex-1 bg-black/40"
            />
            <aside className="flex h-full w-full max-w-md flex-col bg-bg-card shadow-xl">
                <header className="flex items-center justify-between border-b border-border p-4">
                    <div>
                        <h2 className="text-base font-semibold text-text">
                            {user.name || user.email}
                        </h2>
                        <p className="text-xs text-text-muted">
                            {user.email}
                        </p>
                    </div>
                    <button
                        type="button"
                        onClick={onClose}
                        className="rounded px-2 py-1 text-xs hover:bg-bg-card-hover"
                    >
                        Close
                    </button>
                </header>
                <div className="flex-1 overflow-y-auto p-4 space-y-6 text-sm">
                    <BasicsSection
                        user={user}
                        canEdit={canEdit}
                        onPatch={onPatch}
                    />
                    <MotionSection
                        user={user}
                        canEdit={canEdit}
                        onPatch={onPatch}
                    />
                    {canEdit ? <PasswordSection userId={user.id} /> : null}
                    <AuditSection userId={user.id} />
                </div>
            </aside>
        </div>
    );
}

// ─────────────────────────────────────────────────────────────────────
// Basics: name + role
// ─────────────────────────────────────────────────────────────────────

function BasicsSection({
    user,
    canEdit,
    onPatch,
}: {
    user: TeamUser;
    canEdit: boolean;
    onPatch: (id: string, patch: UserPatchPayload) => void;
}) {
    const [name, setName] = useState(user.name ?? "");
    useEffect(() => setName(user.name ?? ""), [user.id, user.name]);
    return (
        <section>
            <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-text-subtle">
                Profile
            </h3>
            <label className="block text-xs">
                Name
                <input
                    type="text"
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                    onBlur={() => {
                        if (canEdit && name !== (user.name ?? "")) {
                            onPatch(user.id, { name: name || undefined });
                        }
                    }}
                    disabled={!canEdit}
                    className="mt-1 w-full rounded border border-border bg-bg-card px-2 py-1 text-sm disabled:opacity-50"
                />
            </label>
            <label className="mt-3 block text-xs">
                Role
                <select
                    value={user.role}
                    onChange={(e) =>
                        canEdit &&
                        onPatch(user.id, { role: e.target.value as TeamUser["role"] })
                    }
                    disabled={!canEdit}
                    className="mt-1 w-full rounded border border-border bg-bg-card px-2 py-1 text-sm disabled:opacity-50"
                >
                    {ROLES.map((r) => (
                        <option key={r} value={r}>
                            {r}
                        </option>
                    ))}
                </select>
                <span className="mt-1 block text-[10px] text-text-subtle">
                    Role is the legacy permission band ("admin" / "manager" /
                    "agent"). Motion scopes below are the load-bearing access
                    rules; role stays around for legacy gates.
                </span>
            </label>
            <p className="mt-3 text-xs text-text-muted">
                Tenant admin:{" "}
                <strong>{user.is_tenant_admin ? "Yes" : "No"}</strong>
                <span className="ml-2 text-[10px] text-text-subtle">
                    (toggle from the Motion access grid on the Team page)
                </span>
            </p>
            <p className="mt-1 text-xs text-text-muted">
                Active: <strong>{user.is_active ? "Yes" : "No"}</strong>
                <span className="ml-2 text-[10px] text-text-subtle">
                    (deactivate / reactivate from the Team page)
                </span>
            </p>
            <p className="mt-1 text-xs text-text-subtle">
                Last seen:{" "}
                {user.last_login_at
                    ? new Date(user.last_login_at).toLocaleString()
                    : "Never"}
                {" · "}Created {new Date(user.created_at).toLocaleDateString()}
            </p>
        </section>
    );
}

// ─────────────────────────────────────────────────────────────────────
// Per-user motion grid (same shape as the page-level grid, but only
// for this user; lets the admin focus without the table noise).
// ─────────────────────────────────────────────────────────────────────

function MotionSection({
    user,
    canEdit,
    onPatch,
}: {
    user: TeamUser;
    canEdit: boolean;
    onPatch: (id: string, patch: UserPatchPayload) => void;
}) {
    const toggle = (
        list: Domain[],
        d: Domain,
        key: "agent_domains" | "manager_domains",
    ) => {
        if (!canEdit) return;
        const next = list.includes(d)
            ? list.filter((x) => x !== d)
            : [...list, d];
        onPatch(user.id, { [key]: next } as Record<string, Domain[]>);
    };

    return (
        <section>
            <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-text-subtle">
                Motion access
            </h3>
            <table className="w-full text-xs">
                <thead>
                    <tr className="text-left text-text-subtle">
                        <th className="pb-1">Motion</th>
                        <th className="pb-1 text-center">Agent</th>
                        <th className="pb-1 text-center">Manager</th>
                    </tr>
                </thead>
                <tbody>
                    {GRID_DOMAINS.map((d) => (
                        <tr key={d} className="border-t border-border">
                            <td className="py-1">{DOMAIN_LABEL[d]}</td>
                            <td className="text-center">
                                <input
                                    type="checkbox"
                                    checked={user.agent_domains.includes(d)}
                                    disabled={!canEdit}
                                    onChange={() =>
                                        toggle(
                                            user.agent_domains,
                                            d,
                                            "agent_domains",
                                        )
                                    }
                                />
                            </td>
                            <td className="text-center">
                                <input
                                    type="checkbox"
                                    checked={user.manager_domains.includes(d)}
                                    disabled={!canEdit}
                                    onChange={() =>
                                        toggle(
                                            user.manager_domains,
                                            d,
                                            "manager_domains",
                                        )
                                    }
                                />
                            </td>
                        </tr>
                    ))}
                </tbody>
            </table>
            <label className="mt-3 flex items-center gap-2">
                <input
                    type="checkbox"
                    checked={user.is_tenant_admin}
                    disabled={!canEdit}
                    onChange={(e) =>
                        canEdit &&
                        onPatch(user.id, { is_tenant_admin: e.target.checked })
                    }
                />
                <span>
                    <strong>Tenant Admin</strong> — Settings + this drawer
                    access. Orthogonal to manager scope.
                </span>
            </label>
        </section>
    );
}

// ─────────────────────────────────────────────────────────────────────
// Password reset
// ─────────────────────────────────────────────────────────────────────

function PasswordSection({ userId }: { userId: string }) {
    const set = useSetUserPassword();
    const [pw, setPw] = useState("");
    const [done, setDone] = useState(false);

    return (
        <section>
            <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-text-subtle">
                Reset password
            </h3>
            <p className="text-xs text-text-muted">
                Sets a new password for this user. We don't email it — copy and
                hand off out of band. The user can change it after sign-in.
            </p>
            <div className="mt-2 flex gap-2">
                <input
                    type="text"
                    value={pw}
                    onChange={(e) => setPw(e.target.value)}
                    placeholder="New password (>= 8 chars)"
                    minLength={8}
                    className="flex-1 rounded border border-border bg-bg-card px-2 py-1 text-sm"
                />
                <button
                    type="button"
                    disabled={set.isPending || pw.length < 8}
                    onClick={async () => {
                        await set.mutateAsync({ id: userId, password: pw });
                        setDone(true);
                        setPw("");
                        setTimeout(() => setDone(false), 4000);
                    }}
                    className="rounded bg-primary px-3 py-1 text-xs font-semibold text-bg disabled:opacity-50"
                >
                    {set.isPending ? "Saving…" : done ? "Saved" : "Reset"}
                </button>
            </div>
        </section>
    );
}

// ─────────────────────────────────────────────────────────────────────
// Audit log (resource_type=user filtered to this user)
// ─────────────────────────────────────────────────────────────────────

function AuditSection({ userId }: { userId: string }) {
    const { data, isLoading } = useUserAuditLog(userId, 50);
    return (
        <section>
            <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-text-subtle">
                Audit log
            </h3>
            {isLoading ? (
                <p className="text-xs text-text-muted">Loading…</p>
            ) : !data || data.items.length === 0 ? (
                <p className="text-xs text-text-muted">
                    No audit entries for this user yet.
                </p>
            ) : (
                <ul className="space-y-2">
                    {data.items.map((row) => (
                        <AuditRow key={row.id} row={row} />
                    ))}
                </ul>
            )}
        </section>
    );
}

function AuditRow({ row }: { row: AuditLogRow }) {
    const summary = humanizeAction(row);
    return (
        <li className="rounded border border-border bg-bg p-2 text-xs">
            <div className="flex items-center justify-between">
                <strong>{summary}</strong>
                <span className="text-text-subtle">
                    {new Date(row.created_at).toLocaleString()}
                </span>
            </div>
            {row.before || row.after ? (
                <details className="mt-1">
                    <summary className="cursor-pointer text-text-muted">
                        diff
                    </summary>
                    <pre className="mt-1 max-h-32 overflow-auto whitespace-pre-wrap font-mono text-[10px] text-text-muted">
                        {JSON.stringify(
                            { before: row.before, after: row.after },
                            null,
                            2,
                        )}
                    </pre>
                </details>
            ) : null}
        </li>
    );
}

function humanizeAction(row: AuditLogRow): string {
    // Dot-namespaced actions become Title-Cased phrases without inventing
    // text — keeps the audit log honest while still readable.
    return row.action.replace(/[_.]/g, " ").replace(
        /\b\w/g,
        (c) => c.toUpperCase(),
    );
}
