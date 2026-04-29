"use client";

import { useState } from "react";
import { useMe } from "@/lib/me";
import type { UserRole } from "@/lib/me";
import {
    SeatReconciliation,
    TeamUser,
    useCreateUser,
    useDeactivateUser,
    usePatchUser,
    useReactivateUser,
    useSeatReconciliation,
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
    const { data: reconciliation } = useSeatReconciliation();
    const patch = usePatchUser();
    const deactivate = useDeactivateUser();
    const reactivate = useReactivateUser();

    const [inviteOpen, setInviteOpen] = useState(false);
    // Whether the picker is needed depends on whether reactivation would
    // exceed the seat cap. Computed in the banner.
    const needsSwap =
        !!reconciliation &&
        reconciliation.active_users >= reconciliation.seat_limit;

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
                {isAdmin && reconciliation?.pending ? (
                    <SeatReconciliationBanner
                        reconciliation={reconciliation}
                        needsSwap={needsSwap}
                        users={users ?? []}
                        onReactivate={(id, swapId) =>
                            reactivate.mutate({
                                id,
                                suspendSwapUserId: swapId,
                            })
                        }
                        pending={reactivate.isPending}
                        error={
                            reactivate.isError
                                ? humanizeError(reactivate.error)
                                : null
                        }
                    />
                ) : null}

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

function SeatReconciliationBanner({
    reconciliation,
    needsSwap,
    users,
    onReactivate,
    pending,
    error,
}: {
    reconciliation: SeatReconciliation;
    needsSwap: boolean;
    users: TeamUser[];
    onReactivate: (id: string, swapId?: string) => void;
    pending: boolean;
    error: string | null;
}) {
    // The "swap victim" must be an active user (not the one we're
    // reactivating). Backend rejects swap-with-self anyway.
    const activeUsers = users.filter((u) => u.is_active);
    const [picked, setPicked] = useState<string>("");
    const [swap, setSwap] = useState<string>("");
    const handleClick = () => {
        if (!picked) return;
        onReactivate(picked, needsSwap ? swap || undefined : undefined);
    };
    return (
        <div className="rounded-lg border border-accent-amber/40 bg-accent-amber/10 p-4">
            <h3 className="text-sm font-semibold">
                Seats need reconciling after a plan change
            </h3>
            <p className="mt-1 text-xs text-text-muted">
                {reconciliation.suspended_users.length} user
                {reconciliation.suspended_users.length === 1 ? "" : "s"}{" "}
                {reconciliation.suspended_users.length === 1 ? "was" : "were"}{" "}
                auto-suspended because the new tier caps you at{" "}
                {reconciliation.seat_limit} active seat
                {reconciliation.seat_limit === 1 ? "" : "s"}. Pick someone to
                bring back below.
                {needsSwap
                    ? " You're at the seat cap — to reactivate someone, choose another active user to suspend in their place."
                    : ""}
            </p>
            {reconciliation.suspended_users.length > 0 ? (
                <div className="mt-3 space-y-2">
                    <label className="block text-xs font-medium">
                        Reactivate
                        <select
                            value={picked}
                            onChange={(e) => setPicked(e.target.value)}
                            className="mt-1 w-full rounded-md border border-border bg-bg-raised px-2 py-1 text-sm"
                        >
                            <option value="">Select a suspended user…</option>
                            {reconciliation.suspended_users.map((u) => (
                                <option key={u.id} value={u.id}>
                                    {u.name || u.email}
                                </option>
                            ))}
                        </select>
                    </label>
                    {needsSwap ? (
                        <label className="block text-xs font-medium">
                            Suspend in their place
                            <select
                                value={swap}
                                onChange={(e) => setSwap(e.target.value)}
                                className="mt-1 w-full rounded-md border border-border bg-bg-raised px-2 py-1 text-sm"
                            >
                                <option value="">
                                    Select an active user…
                                </option>
                                {activeUsers
                                    .filter((u) => u.id !== picked)
                                    .map((u) => (
                                        <option key={u.id} value={u.id}>
                                            {u.name || u.email}
                                        </option>
                                    ))}
                            </select>
                        </label>
                    ) : null}
                    <button
                        type="button"
                        onClick={handleClick}
                        disabled={pending || !picked || (needsSwap && !swap)}
                        className="rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-white disabled:opacity-50"
                    >
                        {pending ? "Reactivating…" : "Reactivate"}
                    </button>
                    {error ? (
                        <p className="text-xs text-accent-rose">{error}</p>
                    ) : null}
                </div>
            ) : (
                <p className="mt-2 text-xs text-text-muted">
                    No suspended users — the banner will dismiss next refresh
                    once the tenant flag clears.
                </p>
            )}
        </div>
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
    // After a successful invite we keep the modal open one beat to show
    // the admin a Copy / Mailto handoff — backend currently has no email
    // provider wired (PRs in flight) so this is the least-bad UX.
    const [created, setCreated] = useState<{
        email: string;
        password: string;
    } | null>(null);

    const reset = () => {
        setCreated(null);
        setEmail("");
        setName("");
        setUserRole("agent");
        setPassword("");
    };

    const close = () => {
        reset();
        onClose();
    };

    const submit = async (e: React.FormEvent) => {
        e.preventDefault();
        try {
            await create.mutateAsync({
                email: email.trim(),
                name: name.trim() || undefined,
                role: userRole,
                password,
            });
            setCreated({ email: email.trim(), password });
        } catch {
            // surfaced inline below
        }
    };

    const mailtoHref = created
        ? `mailto:${encodeURIComponent(created.email)}` +
          `?subject=${encodeURIComponent("Your CallSight account")}` +
          `&body=${encodeURIComponent(
              `An account has been created for you on CallSight.\n\n` +
                  `Sign in at this app's URL using:\n` +
                  `Email: ${created.email}\n` +
                  `Temporary password: ${created.password}\n\n` +
                  `Please change your password after first sign-in.`,
          )}`
        : "";

    return (
        <Modal open={open} onClose={close} title="Invite member">
            {created ? (
                <div className="space-y-3">
                    <p className="text-sm">
                        Invited <strong>{created.email}</strong>. Hand off the
                        sign-in credentials below — we don't email them
                        automatically yet.
                    </p>
                    <div className="rounded-md border border-border bg-bg-raised p-3 text-xs">
                        <div>
                            <span className="text-text-subtle">Email: </span>
                            <span className="font-mono">{created.email}</span>
                        </div>
                        <div className="mt-1">
                            <span className="text-text-subtle">
                                Temporary password:{" "}
                            </span>
                            <span className="font-mono">
                                {created.password}
                            </span>
                        </div>
                    </div>
                    <div className="flex flex-wrap gap-2">
                        <a
                            href={mailtoHref}
                            className="rounded-md border border-border px-3 py-1.5 text-xs hover:bg-bg-raised"
                        >
                            Email credentials
                        </a>
                        <button
                            type="button"
                            onClick={() => {
                                navigator.clipboard?.writeText(
                                    `Email: ${created.email}\nTemporary password: ${created.password}`,
                                );
                            }}
                            className="rounded-md border border-border px-3 py-1.5 text-xs hover:bg-bg-raised"
                        >
                            Copy credentials
                        </button>
                        <button
                            type="button"
                            onClick={close}
                            className="rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-white"
                        >
                            Done
                        </button>
                    </div>
                </div>
            ) : (
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
                            We'll show "Email credentials" / "Copy" buttons
                            after creating the account so you can hand it off
                            directly. The user can change the password after
                            first sign-in.
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
                            onClick={close}
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
            )}
        </Modal>
    );
}
