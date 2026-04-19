"use client";

import { useMe } from "@/lib/me";

export default function DashboardPage() {
    const { data, isLoading, error } = useMe();

    if (isLoading) return <p className="text-text-muted">Loading…</p>;
    if (error || !data) return <p className="text-accent-rose">Couldn&apos;t load your tenant.</p>;

    const { tenant, user } = data;

    return (
        <div className="space-y-6">
            <div>
                <h2 className="text-2xl font-bold">
                    Welcome back{user?.name ? `, ${user.name.split(" ")[0]}` : ""}.
                </h2>
                <p className="text-text-muted mt-1">
                    Here&apos;s your week at a glance — Linda is listening.
                </p>
            </div>

            <section className="grid grid-cols-1 gap-4 sm:grid-cols-3">
                <Stat label="Plan" value={tenant.plan_tier} />
                <Stat label="Role" value={user?.role ?? "—"} />
                <Stat
                    label="Trial"
                    value={
                        tenant.trial_expired
                            ? "Expired"
                            : tenant.trial_active
                              ? `${tenant.trial_ends_at ? new Date(tenant.trial_ends_at).toLocaleDateString() : ""}`
                              : "—"
                    }
                />
            </section>

            <section className="rounded-lg border border-border bg-bg-card p-5">
                <h3 className="text-sm font-semibold text-text-muted mb-3">
                    What&apos;s enabled on {tenant.plan_tier}
                </h3>
                <ul className="grid grid-cols-1 gap-1 text-sm sm:grid-cols-2">
                    {Object.entries(tenant.limits)
                        .filter(([, v]) => typeof v === "boolean")
                        .map(([key, value]) => (
                            <li key={key} className="flex items-center gap-2">
                                <span
                                    className={
                                        value
                                            ? "text-accent-emerald"
                                            : "text-text-subtle"
                                    }
                                >
                                    {value ? "✓" : "—"}
                                </span>
                                <span className="capitalize text-text-muted">
                                    {key.replace(/_/g, " ")}
                                </span>
                            </li>
                        ))}
                </ul>
            </section>
        </div>
    );
}

function Stat({ label, value }: { label: string; value: string }) {
    return (
        <div className="rounded-lg border border-border bg-bg-card p-4">
            <p className="text-xs uppercase tracking-wide text-text-subtle">{label}</p>
            <p className="mt-1 text-lg font-semibold capitalize">{value}</p>
        </div>
    );
}
