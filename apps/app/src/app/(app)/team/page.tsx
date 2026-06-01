"use client";

import { useMemo, useState } from "react";
import { useMe, type Domain } from "@/lib/me";
import type { UserRole } from "@/lib/me";
import {
    SeatReconciliation,
    TeamUser,
    UserImportSummary,
    useCreateUser,
    useDeactivateUser,
    useImportUsers,
    usePatchUser,
    useReactivateUser,
    useSeatReconciliation,
    useSetTenantDefaultMotion,
    useTenantDefaultMotion,
    useUsers,
} from "@/lib/users";
import { useTeamStats } from "@/lib/analytics";
import { DOMAIN_LABEL } from "@/lib/manager";
import {
    ErrorCard,
    ManagerGate,
    Section,
    SkeletonCard,
    humanizeError,
} from "@/components/admin/section";
import { Modal } from "@/components/admin/modal";

const ROLES: UserRole[] = ["agent", "manager", "admin"];
const ALL_DOMAINS: Domain[] = ["sales", "customer_service", "it_support", "generic"];
// Visible domains in the grid header. ``generic`` is admitted by the
// CHECK constraint for legacy migrations but isn't a motion we render
// as a column — managing a generic-only tenant means the columns just
// stay unchecked and the role column is what's load-bearing.
const GRID_DOMAINS: Domain[] = ["sales", "customer_service", "it_support"];

export default function TeamPage() {
    const { data: me } = useMe();
    const role = me?.user?.role;
    const isAdmin = role === "admin" || me?.user?.is_tenant_admin === true;

    const { data: users, isLoading, error } = useUsers(true);
    const { data: stats } = useTeamStats();
    const { data: reconciliation } = useSeatReconciliation();
    const patch = usePatchUser();
    const deactivate = useDeactivateUser();
    const reactivate = useReactivateUser();

    const [inviteOpen, setInviteOpen] = useState(false);
    const [csvOpen, setCsvOpen] = useState(false);
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
                    <div className="flex items-center gap-2">
                        <button
                            type="button"
                            onClick={() => setCsvOpen(true)}
                            className="rounded-md border border-border px-3 py-2 text-sm"
                        >
                            Import CSV
                        </button>
                        <button
                            type="button"
                            onClick={() => setInviteOpen(true)}
                            className="rounded-md bg-primary px-3 py-2 text-sm font-medium text-white"
                        >
                            Invite member
                        </button>
                    </div>
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

                {isAdmin ? <TenantSettingsCard /> : null}

                {error ? (
                    <ErrorCard message={humanizeError(error)} />
                ) : null}

                {isLoading ? (
                    <SkeletonCard />
                ) : (
                    <>
                        <Section
                            title="Members"
                            subtitle="The base record per user. Manage motion access below."
                        >
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
                        </Section>

                        {isAdmin ? (
                            <Section
                                title="Motion access"
                                subtitle="Grant each user agent-level access (they take calls in that motion) and manager-level visibility (they see the dashboard for that motion). Tenant Admin gates Settings access; orthogonal to manager scope."
                            >
                                <MotionAccessGrid
                                    users={users ?? []}
                                    onPatch={(id, p) =>
                                        patch.mutate({ id, patch: p })
                                    }
                                />
                                {patch.isError ? (
                                    <p className="mt-3 text-xs text-accent-rose">
                                        {humanizeError(patch.error)}
                                    </p>
                                ) : null}
                            </Section>
                        ) : null}
                    </>
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
                                                : "-"}
                                        </td>
                                        <td className="py-2">
                                            {s.avg_scorecard_score != null
                                                ? s.avg_scorecard_score.toFixed(
                                                      1,
                                                  )
                                                : "-"}
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
            <CsvImportModal open={csvOpen} onClose={() => setCsvOpen(false)} />
        </div>
    );
}

// ─────────────────────────────────────────────────────────────────────
// Tenant settings (default motion picker)
// ─────────────────────────────────────────────────────────────────────

function TenantSettingsCard() {
    const { data, isLoading } = useTenantDefaultMotion();
    const set = useSetTenantDefaultMotion();
    const [pending, setPending] = useState<Domain | null>(null);

    if (isLoading || !data) return null;

    const onChange = async (next: Domain) => {
        if (next === data.default_domain) return;
        setPending(next);
        try {
            await set.mutateAsync({ default_domain: next });
        } finally {
            setPending(null);
        }
    };

    return (
        <Section
            title="Tenant settings"
            subtitle="The primary motion this tenant runs. Drives the default motion assignment for new invites; never retroactively changes anyone's access."
        >
            <div className="flex flex-wrap items-center gap-2">
                <label className="text-sm font-medium" htmlFor="default-motion">
                    Primary motion:
                </label>
                <select
                    id="default-motion"
                    value={data.default_domain}
                    onChange={(e) => onChange(e.target.value as Domain)}
                    disabled={set.isPending}
                    className="rounded-md border border-border bg-bg-raised px-3 py-1.5 text-sm"
                >
                    {ALL_DOMAINS.map((d) => (
                        <option key={d} value={d}>
                            {DOMAIN_LABEL[d]}
                        </option>
                    ))}
                </select>
                {pending && set.isPending ? (
                    <span className="text-xs text-text-subtle">Saving…</span>
                ) : null}
                {set.isError ? (
                    <span className="text-xs text-accent-rose">
                        {humanizeError(set.error)}
                    </span>
                ) : null}
            </div>
        </Section>
    );
}

// ─────────────────────────────────────────────────────────────────────
// Motion access grid
// ─────────────────────────────────────────────────────────────────────

function MotionAccessGrid({
    users,
    onPatch,
}: {
    users: TeamUser[];
    onPatch: (
        id: string,
        patch: {
            agent_domains?: Domain[];
            manager_domains?: Domain[];
            is_tenant_admin?: boolean;
        },
    ) => void;
}) {
    return (
        <div className="overflow-x-auto">
            <table className="w-full min-w-[720px] text-sm">
                <thead>
                    <tr className="text-left text-xs uppercase tracking-wide text-text-subtle">
                        <th className="pb-2">User</th>
                        {GRID_DOMAINS.map((d) => (
                            <th
                                key={`agent-${d}`}
                                className="pb-2 text-center"
                                title={`Agent: works front-line in ${DOMAIN_LABEL[d]}`}
                            >
                                {DOMAIN_LABEL[d]}
                                <br />
                                <span className="text-[10px] font-normal normal-case text-text-subtle">
                                    Agent
                                </span>
                            </th>
                        ))}
                        {GRID_DOMAINS.map((d) => (
                            <th
                                key={`mgr-${d}`}
                                className="pb-2 text-center"
                                title={`Manager: sees the ${DOMAIN_LABEL[d]} dashboard`}
                            >
                                {DOMAIN_LABEL[d]}
                                <br />
                                <span className="text-[10px] font-normal normal-case text-text-subtle">
                                    Manager
                                </span>
                            </th>
                        ))}
                        <th
                            className="pb-2 text-center"
                            title="Can manage tenant settings + this user grid. Orthogonal to manager scope."
                        >
                            Tenant
                            <br />
                            <span className="text-[10px] font-normal normal-case text-text-subtle">
                                Admin
                            </span>
                        </th>
                    </tr>
                </thead>
                <tbody>
                    {users.map((u) => (
                        <MotionRow key={u.id} user={u} onPatch={onPatch} />
                    ))}
                </tbody>
            </table>
            <p className="mt-3 text-xs text-text-subtle">
                A user with two or more "Manager" boxes checked also sees the
                cross-motion Journey view in the Manager portal.
            </p>
        </div>
    );
}

function MotionRow({
    user,
    onPatch,
}: {
    user: TeamUser;
    onPatch: (
        id: string,
        patch: {
            agent_domains?: Domain[];
            manager_domains?: Domain[];
            is_tenant_admin?: boolean;
        },
    ) => void;
}) {
    // Local optimistic state so the checkbox flips instantly. Falls back
    // to the canonical server value when ``user`` refreshes after the
    // patch resolves.
    const [optAgent, setOptAgent] = useState<Domain[] | null>(null);
    const [optManager, setOptManager] = useState<Domain[] | null>(null);
    const [optAdmin, setOptAdmin] = useState<boolean | null>(null);

    const agent = optAgent ?? user.agent_domains;
    const manager = optManager ?? user.manager_domains;
    const admin = optAdmin ?? user.is_tenant_admin;

    const toggle = (
        list: Domain[],
        d: Domain,
        setter: (next: Domain[]) => void,
        key: "agent_domains" | "manager_domains",
    ) => {
        const next = list.includes(d)
            ? list.filter((x) => x !== d)
            : [...list, d];
        setter(next);
        onPatch(user.id, { [key]: next } as Record<string, Domain[]>);
    };

    if (!user.is_active) {
        return (
            <tr className="border-t border-border opacity-60">
                <td className="py-2">
                    {user.name || user.email}
                    <span className="ml-2 text-xs text-text-subtle">
                        (inactive)
                    </span>
                </td>
                <td colSpan={GRID_DOMAINS.length * 2 + 1} className="text-xs text-text-subtle">
                    Reactivate to manage motion access.
                </td>
            </tr>
        );
    }

    return (
        <tr className="border-t border-border">
            <td className="py-2 pr-3">
                <div>{user.name || user.email.split("@")[0]}</div>
                <div className="text-xs text-text-subtle">{user.email}</div>
            </td>
            {GRID_DOMAINS.map((d) => (
                <td key={`agent-${d}`} className="text-center">
                    <input
                        type="checkbox"
                        checked={agent.includes(d)}
                        onChange={() =>
                            toggle(agent, d, setOptAgent, "agent_domains")
                        }
                        aria-label={`${user.email} agent in ${d}`}
                    />
                </td>
            ))}
            {GRID_DOMAINS.map((d) => (
                <td key={`mgr-${d}`} className="text-center">
                    <input
                        type="checkbox"
                        checked={manager.includes(d)}
                        onChange={() =>
                            toggle(manager, d, setOptManager, "manager_domains")
                        }
                        aria-label={`${user.email} manager of ${d}`}
                    />
                </td>
            ))}
            <td className="text-center">
                <input
                    type="checkbox"
                    checked={admin}
                    onChange={() => {
                        const next = !admin;
                        setOptAdmin(next);
                        onPatch(user.id, { is_tenant_admin: next });
                    }}
                    aria-label={`${user.email} is tenant admin`}
                />
            </td>
        </tr>
    );
}

// ─────────────────────────────────────────────────────────────────────
// User row + seat reconciliation banner (unchanged shape, kept inline)
// ─────────────────────────────────────────────────────────────────────

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
                    ? " You're at the seat cap. To reactivate someone, choose another active user to suspend in their place."
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
                    No suspended users. The banner will dismiss next refresh
                    once the tenant flag clears.
                </p>
            )}
        </div>
    );
}

// ─────────────────────────────────────────────────────────────────────
// Invite modal (now motion-aware)
// ─────────────────────────────────────────────────────────────────────

function InviteModal({
    open,
    onClose,
}: {
    open: boolean;
    onClose: () => void;
}) {
    const create = useCreateUser();
    const { data: tenantDefault } = useTenantDefaultMotion();
    const fallback: Domain = tenantDefault?.default_domain ?? "sales";

    const [email, setEmail] = useState("");
    const [name, setName] = useState("");
    const [userRole, setUserRole] = useState<UserRole>("agent");
    const [password, setPassword] = useState("");
    const [agentDomains, setAgentDomains] = useState<Domain[]>([fallback]);
    const [managerDomains, setManagerDomains] = useState<Domain[]>([]);
    const [tenantAdmin, setTenantAdmin] = useState(false);
    const [created, setCreated] = useState<{
        email: string;
        password: string;
    } | null>(null);

    // When the role changes, seed sensible defaults so the admin
    // doesn't have to tick the obvious boxes for the common case.
    useMemo(() => {
        if (userRole === "admin") {
            setTenantAdmin(true);
            if (managerDomains.length === 0) {
                setManagerDomains([fallback]);
            }
        }
        // Intentional dep list: we only seed when the role flips.
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [userRole]);

    const reset = () => {
        setCreated(null);
        setEmail("");
        setName("");
        setUserRole("agent");
        setPassword("");
        setAgentDomains([fallback]);
        setManagerDomains([]);
        setTenantAdmin(false);
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
                agent_domains: agentDomains,
                manager_domains: managerDomains,
                is_tenant_admin: tenantAdmin,
            });
            setCreated({ email: email.trim(), password });
        } catch {
            // surfaced inline below
        }
    };

    const toggle = (
        list: Domain[],
        d: Domain,
        setter: (next: Domain[]) => void,
    ) => {
        setter(list.includes(d) ? list.filter((x) => x !== d) : [...list, d]);
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
                    <fieldset className="rounded-md border border-border p-3">
                        <legend className="px-1 text-xs font-semibold uppercase tracking-wide text-text-subtle">
                            Motion access
                        </legend>
                        <p className="mb-2 text-xs text-text-subtle">
                            Tick a "Sees dashboard" box to grant manager-level
                            visibility into that motion. Two or more unlocks
                            the cross-motion Journey view.
                        </p>
                        <div className="grid grid-cols-2 gap-x-3 gap-y-1 text-xs">
                            <div className="font-semibold text-text-subtle">
                                Takes calls in
                            </div>
                            <div className="font-semibold text-text-subtle">
                                Sees dashboard for
                            </div>
                            {GRID_DOMAINS.map((d) => (
                                <label
                                    key={`a-${d}`}
                                    className="flex items-center gap-1.5"
                                >
                                    <input
                                        type="checkbox"
                                        checked={agentDomains.includes(d)}
                                        onChange={() =>
                                            toggle(
                                                agentDomains,
                                                d,
                                                setAgentDomains,
                                            )
                                        }
                                    />
                                    {DOMAIN_LABEL[d]}
                                </label>
                            ))}
                            <div></div>
                            {GRID_DOMAINS.map((d, i) => (
                                <span key={`spacer-${d}-${i}`} />
                            ))}
                            {GRID_DOMAINS.map((d) => (
                                <label
                                    key={`m-${d}`}
                                    className="flex items-center gap-1.5"
                                    style={{ gridColumn: 2 }}
                                >
                                    <input
                                        type="checkbox"
                                        checked={managerDomains.includes(d)}
                                        onChange={() =>
                                            toggle(
                                                managerDomains,
                                                d,
                                                setManagerDomains,
                                            )
                                        }
                                    />
                                    {DOMAIN_LABEL[d]}
                                </label>
                            ))}
                        </div>
                        <label className="mt-3 flex items-center gap-1.5 text-xs">
                            <input
                                type="checkbox"
                                checked={tenantAdmin}
                                onChange={(e) =>
                                    setTenantAdmin(e.target.checked)
                                }
                            />
                            <span>
                                <strong>Tenant Admin</strong> — can manage
                                Settings and this grid. Orthogonal to manager
                                scope.
                            </span>
                        </label>
                    </fieldset>
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

// ─────────────────────────────────────────────────────────────────────
// CSV import modal
// ─────────────────────────────────────────────────────────────────────

function CsvImportModal({
    open,
    onClose,
}: {
    open: boolean;
    onClose: () => void;
}) {
    const importer = useImportUsers();
    const [file, setFile] = useState<File | null>(null);
    const [result, setResult] = useState<UserImportSummary | null>(null);
    const [error, setError] = useState<string | null>(null);

    const reset = () => {
        setFile(null);
        setResult(null);
        setError(null);
    };
    const close = () => {
        reset();
        onClose();
    };

    const submit = async (e: React.FormEvent) => {
        e.preventDefault();
        if (!file) return;
        setError(null);
        try {
            const summary = await importer.mutateAsync(file);
            setResult(summary);
        } catch (err) {
            setError(err instanceof Error ? err.message : String(err));
        }
    };

    return (
        <Modal open={open} onClose={close} title="Import users from CSV">
            {result ? (
                <div className="space-y-3 text-sm">
                    <p>
                        <strong>{result.created}</strong> created,{" "}
                        <strong>{result.skipped}</strong> skipped out of{" "}
                        {result.total_rows} rows.
                    </p>
                    {result.rows.some((r) => r.error) ? (
                        <details className="rounded border border-border p-2 text-xs">
                            <summary className="cursor-pointer font-semibold">
                                See errors
                            </summary>
                            <ul className="mt-2 space-y-1">
                                {result.rows
                                    .filter((r) => r.error)
                                    .map((r) => (
                                        <li
                                            key={r.line_number}
                                            className="font-mono"
                                        >
                                            Line {r.line_number}
                                            {r.email ? ` (${r.email})` : ""}:{" "}
                                            {r.error}
                                        </li>
                                    ))}
                            </ul>
                        </details>
                    ) : null}
                    <div className="flex justify-end gap-2">
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
                    <p className="text-sm text-text-muted">
                        Upload a CSV with header columns:{" "}
                        <span className="font-mono">email</span>,{" "}
                        <span className="font-mono">name</span>,{" "}
                        <span className="font-mono">role</span>,{" "}
                        <span className="font-mono">password</span>,{" "}
                        <span className="font-mono">agent_domains</span>,{" "}
                        <span className="font-mono">manager_domains</span>,{" "}
                        <span className="font-mono">is_tenant_admin</span>.
                        Motion columns are pipe-delimited
                        (e.g. <span className="font-mono">sales|customer_service</span>).
                        200-row cap per file.
                    </p>
                    <input
                        type="file"
                        accept=".csv,text/csv"
                        onChange={(e) => setFile(e.target.files?.[0] ?? null)}
                        required
                        className="w-full text-sm"
                    />
                    {error ? (
                        <p className="text-xs text-accent-rose">{error}</p>
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
                            disabled={importer.isPending || !file}
                            className="rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-white disabled:opacity-50"
                        >
                            {importer.isPending ? "Importing…" : "Import"}
                        </button>
                    </div>
                </form>
            )}
        </Modal>
    );
}
