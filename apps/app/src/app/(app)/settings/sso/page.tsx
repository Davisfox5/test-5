"use client";

/**
 * SSO group → motion-scope mapping (Settings → SSO).
 *
 * Tenant admins manage which IDP groups grant which motion scopes
 * when a user signs in via SSO or is pushed via SCIM. The "Test"
 * panel dry-runs a group list through the resolver so the admin can
 * confirm a user with those groups would land where they expect
 * before saving anything.
 *
 * Gated on ``is_tenant_admin``; non-admins see an empty state
 * pointing back to Settings.
 */

import { useState } from "react";
import { useMe, type Domain } from "@/lib/me";
import { DOMAIN_LABEL } from "@/lib/manager";
import {
    MotionRule,
    useCreateMotionRule,
    useDeleteMotionRule,
    useMotionRules,
    usePatchMotionRule,
    useTestResolveScopes,
} from "@/lib/sso";

const GRID_DOMAINS: Domain[] = ["sales", "customer_service", "it_support"];

export default function SsoSettingsPage() {
    const me = useMe();
    const isAdmin =
        me.data?.user?.is_tenant_admin === true ||
        me.data?.user?.role === "admin";

    const { data: rules = [], isLoading } = useMotionRules();
    const create = useCreateMotionRule();
    const patch = usePatchMotionRule();
    const del = useDeleteMotionRule();

    if (me.isLoading) return <p className="text-text-muted">Loading…</p>;
    if (!isAdmin) {
        return (
            <div className="rounded-lg border border-border bg-bg-card p-4">
                <p className="text-text">You need tenant-admin access to manage SSO.</p>
            </div>
        );
    }

    return (
        <div className="space-y-6">
            <header className="space-y-1">
                <h1 className="text-2xl font-bold">SSO &amp; SCIM</h1>
                <p className="text-sm text-text-muted">
                    Map identity-provider groups (Okta / Azure AD / Workspace) to
                    motion scopes. Rules apply at SSO login and on every SCIM
                    push from your IDP.
                </p>
            </header>

            <NewRuleCard onCreate={(body) => create.mutate(body)} />

            <section>
                <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-text-muted">
                    Rules ({rules.length})
                </h2>
                <div className="rounded-lg border border-border bg-bg-card">
                    {isLoading ? (
                        <p className="p-4 text-sm text-text-muted">Loading…</p>
                    ) : rules.length === 0 ? (
                        <p className="p-4 text-sm text-text-muted">
                            No rules yet. Add one above to start mapping IDP
                            groups to motion scopes.
                        </p>
                    ) : (
                        <ul className="divide-y divide-border">
                            {rules.map((r) => (
                                <RuleRow
                                    key={r.id}
                                    rule={r}
                                    onPatch={(p) =>
                                        patch.mutate({ id: r.id, patch: p })
                                    }
                                    onDelete={() => {
                                        if (
                                            confirm(
                                                `Delete rule for "${r.group_name}"?`,
                                            )
                                        )
                                            del.mutate(r.id);
                                    }}
                                />
                            ))}
                        </ul>
                    )}
                </div>
            </section>

            <TestResolveCard />
        </div>
    );
}

function NewRuleCard({
    onCreate,
}: {
    onCreate: (body: {
        group_name: string;
        agent_domains: Domain[];
        manager_domains: Domain[];
        grants_tenant_admin: boolean;
    }) => void;
}) {
    const [group, setGroup] = useState("");
    const [agent, setAgent] = useState<Domain[]>([]);
    const [manager, setManager] = useState<Domain[]>([]);
    const [admin, setAdmin] = useState(false);

    const reset = () => {
        setGroup("");
        setAgent([]);
        setManager([]);
        setAdmin(false);
    };

    const submit = (e: React.FormEvent) => {
        e.preventDefault();
        if (!group.trim()) return;
        onCreate({
            group_name: group.trim(),
            agent_domains: agent,
            manager_domains: manager,
            grants_tenant_admin: admin,
        });
        reset();
    };

    const toggle = (list: Domain[], d: Domain, setter: (next: Domain[]) => void) =>
        setter(list.includes(d) ? list.filter((x) => x !== d) : [...list, d]);

    return (
        <section className="rounded-lg border border-border bg-bg-card p-4">
            <h2 className="text-sm font-semibold uppercase tracking-wide text-text-muted">
                Add rule
            </h2>
            <form onSubmit={submit} className="mt-3 space-y-3 text-sm">
                <label className="block">
                    Group name
                    <input
                        type="text"
                        value={group}
                        onChange={(e) => setGroup(e.target.value)}
                        placeholder="linda-sales-agents"
                        className="mt-1 w-full rounded border border-border bg-bg-card px-2 py-1 text-sm"
                    />
                    <span className="mt-1 block text-[10px] text-text-subtle">
                        Whatever string your IDP emits in the user's groups
                        claim or SCIM groups payload (Okta group name, Azure AD
                        group object id, Workspace org-unit path, etc.).
                    </span>
                </label>
                <div className="grid grid-cols-2 gap-3 text-xs">
                    <div>
                        <div className="mb-1 font-semibold text-text-subtle">
                            Agent in
                        </div>
                        {GRID_DOMAINS.map((d) => (
                            <label
                                key={`a-${d}`}
                                className="flex items-center gap-1.5"
                            >
                                <input
                                    type="checkbox"
                                    checked={agent.includes(d)}
                                    onChange={() => toggle(agent, d, setAgent)}
                                />
                                {DOMAIN_LABEL[d]}
                            </label>
                        ))}
                    </div>
                    <div>
                        <div className="mb-1 font-semibold text-text-subtle">
                            Manager of
                        </div>
                        {GRID_DOMAINS.map((d) => (
                            <label
                                key={`m-${d}`}
                                className="flex items-center gap-1.5"
                            >
                                <input
                                    type="checkbox"
                                    checked={manager.includes(d)}
                                    onChange={() =>
                                        toggle(manager, d, setManager)
                                    }
                                />
                                {DOMAIN_LABEL[d]}
                            </label>
                        ))}
                    </div>
                </div>
                <label className="flex items-center gap-1.5 text-xs">
                    <input
                        type="checkbox"
                        checked={admin}
                        onChange={(e) => setAdmin(e.target.checked)}
                    />
                    <span>
                        <strong>Tenant Admin</strong> — grants Settings access.
                    </span>
                </label>
                <button
                    type="submit"
                    disabled={!group.trim()}
                    className="rounded bg-primary px-3 py-1 text-xs font-semibold text-bg disabled:opacity-50"
                >
                    Add rule
                </button>
            </form>
        </section>
    );
}

function RuleRow({
    rule,
    onPatch,
    onDelete,
}: {
    rule: MotionRule;
    onPatch: (p: Partial<MotionRule>) => void;
    onDelete: () => void;
}) {
    const toggle = (
        list: Domain[],
        d: Domain,
        key: "agent_domains" | "manager_domains",
    ) =>
        onPatch({
            [key]: list.includes(d) ? list.filter((x) => x !== d) : [...list, d],
        } as Partial<MotionRule>);

    return (
        <li className="p-3">
            <div className="flex items-start justify-between gap-3">
                <div className="space-y-1">
                    <p className="text-sm font-medium text-text">
                        {rule.group_name}
                    </p>
                    <div className="flex flex-wrap gap-x-3 gap-y-1 text-xs">
                        {GRID_DOMAINS.map((d) => (
                            <label
                                key={`a-${d}`}
                                className="flex items-center gap-1"
                            >
                                <input
                                    type="checkbox"
                                    checked={rule.agent_domains.includes(d)}
                                    onChange={() =>
                                        toggle(
                                            rule.agent_domains,
                                            d,
                                            "agent_domains",
                                        )
                                    }
                                />
                                Agent {DOMAIN_LABEL[d]}
                            </label>
                        ))}
                        {GRID_DOMAINS.map((d) => (
                            <label
                                key={`m-${d}`}
                                className="flex items-center gap-1"
                            >
                                <input
                                    type="checkbox"
                                    checked={rule.manager_domains.includes(d)}
                                    onChange={() =>
                                        toggle(
                                            rule.manager_domains,
                                            d,
                                            "manager_domains",
                                        )
                                    }
                                />
                                Manager {DOMAIN_LABEL[d]}
                            </label>
                        ))}
                        <label className="flex items-center gap-1">
                            <input
                                type="checkbox"
                                checked={rule.grants_tenant_admin}
                                onChange={(e) =>
                                    onPatch({
                                        grants_tenant_admin: e.target.checked,
                                    })
                                }
                            />
                            Tenant Admin
                        </label>
                        <label className="flex items-center gap-1">
                            <input
                                type="checkbox"
                                checked={rule.is_active}
                                onChange={(e) =>
                                    onPatch({ is_active: e.target.checked })
                                }
                            />
                            Active
                        </label>
                    </div>
                </div>
                <button
                    type="button"
                    onClick={onDelete}
                    className="text-xs text-accent-rose hover:underline"
                >
                    Delete
                </button>
            </div>
        </li>
    );
}

function TestResolveCard() {
    const test = useTestResolveScopes();
    const [input, setInput] = useState("");
    const groups = input
        .split(/[\s,]+/)
        .map((s) => s.trim())
        .filter(Boolean);

    return (
        <section className="rounded-lg border border-border bg-bg-card p-4">
            <h2 className="text-sm font-semibold uppercase tracking-wide text-text-muted">
                Test resolve
            </h2>
            <p className="mt-1 text-xs text-text-muted">
                Paste a comma-separated list of IDP group names. We'll run them
                through your rules and show the scopes a user with those groups
                would land with. Nothing is saved or changed.
            </p>
            <textarea
                value={input}
                onChange={(e) => setInput(e.target.value)}
                rows={2}
                placeholder="linda-sales-agents, linda-cs-managers"
                className="mt-2 w-full rounded border border-border bg-bg-card px-2 py-1 text-sm"
            />
            <button
                type="button"
                onClick={() => test.mutate(groups)}
                disabled={!groups.length || test.isPending}
                className="mt-2 rounded border border-border bg-bg-card px-3 py-1 text-xs hover:bg-bg-card-hover disabled:opacity-50"
            >
                {test.isPending ? "Resolving…" : "Resolve"}
            </button>
            {test.data && (
                <div className="mt-3 rounded border border-border bg-bg p-2 text-xs">
                    <p>
                        Matched <strong>{test.data.matched_rule_count}</strong>{" "}
                        rule(s).
                    </p>
                    <p className="mt-1">
                        Agent domains:{" "}
                        <strong>
                            {test.data.agent_domains.length
                                ? test.data.agent_domains
                                      .map((d) => DOMAIN_LABEL[d])
                                      .join(", ")
                                : "none"}
                        </strong>
                    </p>
                    <p>
                        Manager domains:{" "}
                        <strong>
                            {test.data.manager_domains.length
                                ? test.data.manager_domains
                                      .map((d) => DOMAIN_LABEL[d])
                                      .join(", ")
                                : "none"}
                        </strong>
                    </p>
                    <p>
                        Tenant admin:{" "}
                        <strong>
                            {test.data.is_tenant_admin ? "Yes" : "No"}
                        </strong>
                    </p>
                </div>
            )}
        </section>
    );
}
