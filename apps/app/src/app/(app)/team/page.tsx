"use client";

import { useState } from "react";
import { useMe } from "@/lib/me";
import type { UserRole } from "@/lib/me";
import {
    TeamUser,
    useCreateUser,
    useDeactivateUser,
    usePatchUser,
    useReactivateUser,
    useUsers,
} from "@/lib/users";
import { useTeamStats } from "@/lib/analytics";
import {
    ErrorCard,
    ManagerGate,
    Section,
    SkeletonCard,
    humanizeError,
} from "@/components/admin/section";
import { Modal } from "@/components/admin/modal";

const ROLES: UserRole[] = ["agent", "manager", "admin"];

export default function TeamPage() {
    const { data: me } = useMe();
    const role = me?.user?.role;
    const isAdmin = role === "admin";

    const { data: users, isLoading, error } = useUsers(true);
    const { data: stats } = useTeamStats();
    const patch = usePatchUser();
    const deactivate = useDeactivateUser();
    const reactivate = useReactivateUser();

    const [inviteOpen, setInviteOpen] = useState(false);

    return (
        <div className="space-y-6">
            <header className="flex items-start justify-between gap-4">
                <div>
                    <h2 className="text-2xl font-bold">Team</h2>
                    <p className="text-text-muted mt-1">
                        Active members of {me?.tenant.name ?? "your tenant"}.
                    </p>
                </div>
                {isAdmin ? (
                    <button
                        type="button"
                        onClick={() => setInviteOpen(true)}
                        className="rounded-md bg-primary px-3 py-2 text-sm font-medium text-white"
                    >
                        Invite member
                    </button>
                ) : null}
            </header>

            <ManagerGate role={role}>
                {error ? (
                    <ErrorCard message={humanizeError(error)} />
                ) : null}

                {isLoading ? (
                    <SkeletonCard />
                ) : (
                    <Section title="Members" subtitle="">
                        <table className="w-full text-sm">
                            <thead>
                                <tr className="text-left text-xs uppercase tracking-wide text-text-subtle">
                                    <th className="pb-2">Name</th>
                                    <th className="pb-2">Email</th>
                                    <th className="pb-2">Role</th>
                                    <th className="pb-2">Active</th>
                                    <th className="pb-2">Last seen</th>
                                    <th className="pb-2 sr-only">Actions</th>
                                </tr>
                            </thead>
                            <tbody>
                                {(users ?? []).map((u) => (
                                    <UserRow
                                        key={u.id}
                                        user={u}
                                        canEdit={isAdmin}
                                        onChangeRole={(next) =>
                                            patch.mutate({
                                                id: u.id,
                                                patch: { role: next },
                                            })
                                        }
                                        onDeactivate={() => {
                                            if (
                                                confirm(
                                                    `Deactivate ${u.email}?`,
                                                )
                                            )
                                                deactivate.mutate(u.id);
                                        }}
                                        onReactivate={() =>
                                            reactivate.mutate({ id: u.id })
                                        }
                                    />
                                ))}
                            </tbody>
                        </table>
                        {!users?.length ? (
                            <p className="mt-3 text-sm text-text-muted">
                                No team members yet.
                            </p>
                        ) : null}
                        {patch.isError ? (
                            <p className="mt-3 text-xs text-accent-rose">
                                {humanizeError(patch.error)}
                            </p>
                        ) : null}
                        {deactivate.isError ? (
                            <p className="mt-3 text-xs text-accent-rose">
                                {humanizeError(deactivate.error)}
                            </p>
                        ) : null}
                        {reactivate.isError ? (
                            <p className="mt-3 text-xs text-accent-rose">
                                {humanizeError(reactivate.error)}
                            </p>
                        ) : null}
                    </Section>
                )}

                <Section
                    title="Team activity"
                    subtitle="Per-agent volume and average sentiment over the last 7 days."
                >
                    {stats === undefined ? (
                        <p className="text-sm text-text-muted">Loading…</p>
                    ) : !stats?.length ? (
                        <p className="text-sm text-text-muted">
                            No interactions logged yet for any agent.
                        </p>
                    ) : (
                        <table className="w-full text-sm">
                            <thead>
                                <tr className="text-left text-xs uppercase tracking-wide text-text-subtle">
                                    <th className="pb-2">Agent</th>
                                    <th className="pb-2">Interactions</th>
                                    <th className="pb-2">Avg sentiment</th>
                                    <th className="pb-2">Avg QA</th>
                                    <th className="pb-2">Churn flags</th>
                                </tr>
                            </thead>
                            <tbody>
                                {stats.map((s) => (
                                    <tr
                                        key={s.agent_id}
                                        className="border-t border-border"
                                    >
                                        <td className="py-2">
                                            {s.name ?? <em>unnamed</em>}
                                        </td>
                                        <td className="py-2">
                                            {s.interaction_count}
                                        </td>
                                        <td className="py-2">
                                            {s.avg_sentiment != null
                                                ? s.avg_sentiment.toFixed(2)
                                                : "—"}
                                        </td>
                                        <td className="py-2">
                                            {s.avg_scorecard_score != null
                                                ? s.avg_scorecard_score.toFixed(
                                                      1,
                                                  )
                                                : "—"}
                                        </td>
                                        <td className="py-2">
                                            {s.churn_flags}
                                        </td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    )}
                </Section>
            </ManagerGate>

            <InviteModal
                open={inviteOpen}
                onClose={() => setInviteOpen(false)}
            />
        </div>
    );
}

function UserRow({
    user,
    canEdit,
    onChangeRole,
    onDeactivate,
    onReactivate,
}: {
    user: TeamUser;
    canEdit: boolean;
    onChangeRole: (next: UserRole) => void;
    onDeactivate: () => void;
    onReactivate: () => void;
}) {
    return (
        <tr className="border-t border-border align-middle">
            <td className="py-2">{user.name ?? <em>unnamed</em>}</td>
            <td className="py-2 text-text-muted">{user.email}</td>
            <td className="py-2">
                {canEdit ? (
                    <select
                        value={user.role}
                        onChange={(e) =>
                            onChangeRole(e.target.value as UserRole)
                        }
                        className="rounded-md border border-border bg-bg-raised px-2 py-1 text-xs"
                    >
                        {ROLES.map((r) => (
                            <option key={r} value={r}>
                                {r}
                            </option>
                        ))}
                    </select>
                ) : (
                    <span className="capitalize">{user.role}</span>
                )}
            </td>
            <td className="py-2">
                {user.is_active ? (
                    <span className="text-accent-emerald">Active</span>
                ) : (
                    <span className="text-text-subtle">Inactive</span>
                )}
            </td>
            <td className="py-2 text-xs text-text-subtle">
                {user.last_login_at
                    ? new Date(user.last_login_at).toLocaleString()
                    : "Never"}
            </td>
            <td className="py-2 text-right">
                {canEdit ? (
                    user.is_active ? (
                        <button
                            type="button"
                            onClick={onDeactivate}
                            className="text-xs text-accent-rose hover:underline"
                        >
                            Deactivate
                        </button>
                    ) : (
                        <button
                            type="button"
                            onClick={onReactivate}
                            className="text-xs text-primary hover:underline"
                        >
                            Reactivate
                        </button>
                    )
                ) : null}
            </td>
        </tr>
    );
}

function InviteModal({
    open,
    onClose,
}: {
    open: boolean;
    onClose: () => void;
}) {
    const create = useCreateUser();
    const [email, setEmail] = useState("");
    const [name, setName] = useState("");
    const [userRole, setUserRole] = useState<UserRole>("agent");
    const [password, setPassword] = useState("");

    const submit = async (e: React.FormEvent) => {
        e.preventDefault();
        try {
            await create.mutateAsync({
                email: email.trim(),
                name: name.trim() || undefined,
                role: userRole,
                password,
            });
            onClose();
            setEmail("");
            setName("");
            setUserRole("agent");
            setPassword("");
        } catch {
            // surfaced inline below
        }
    };

    return (
        <Modal open={open} onClose={onClose} title="Invite member">
            <form onSubmit={submit} className="space-y-3">
                <label className="block text-sm font-medium">
                    Email
                    <input
                        type="email"
                        required
                        value={email}
                        onChange={(e) => setEmail(e.target.value)}
                        className="mt-1 w-full rounded-md border border-border bg-bg-raised px-3 py-2 text-sm"
                    />
                </label>
                <label className="block text-sm font-medium">
                    Name (optional)
                    <input
                        type="text"
                        value={name}
                        onChange={(e) => setName(e.target.value)}
                        className="mt-1 w-full rounded-md border border-border bg-bg-raised px-3 py-2 text-sm"
                    />
                </label>
                <label className="block text-sm font-medium">
                    Role
                    <select
                        value={userRole}
                        onChange={(e) =>
                            setUserRole(e.target.value as UserRole)
                        }
                        className="mt-1 w-full rounded-md border border-border bg-bg-raised px-3 py-2 text-sm"
                    >
                        {ROLES.map((r) => (
                            <option key={r} value={r}>
                                {r}
                            </option>
                        ))}
                    </select>
                </label>
                <label className="block text-sm font-medium">
                    Initial password
                    <input
                        type="text"
                        required
                        minLength={8}
                        value={password}
                        onChange={(e) => setPassword(e.target.value)}
                        className="mt-1 w-full rounded-md border border-border bg-bg-raised px-3 py-2 text-sm"
                    />
                    <span className="mt-1 block text-xs text-text-subtle">
                        Send out-of-band — the user can change it after first
                        sign-in.
                    </span>
                </label>
                {create.isError ? (
                    <p className="text-xs text-accent-rose">
                        {humanizeError(create.error)}
                    </p>
                ) : null}
                <div className="flex justify-end gap-2">
                    <button
                        type="button"
                        onClick={onClose}
                        className="rounded-md border border-border px-3 py-1.5 text-xs"
                    >
                        Cancel
                    </button>
                    <button
                        type="submit"
                        disabled={create.isPending}
                        className="rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-white disabled:opacity-50"
                    >
                        {create.isPending ? "Inviting…" : "Invite"}
                    </button>
                </div>
            </form>
        </Modal>
    );
}
