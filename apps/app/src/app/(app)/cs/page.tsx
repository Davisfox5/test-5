"use client";

/**
 * Customer Success portal — real renewals + alerts wiring.
 *
 * Replaces the PR #113 scaffold. Shows the upcoming-renewals strip
 * with risk badges, the CS-motion manager alerts, and a recent-CS-
 * calls quick link. Account drill-down lives at /cs/accounts/[id].
 */

import Link from "next/link";
import { useState } from "react";
import { useMe } from "@/lib/me";
import { useManagerAlerts, type ManagerAlert } from "@/lib/manager";
import {
    riskBand,
    useUpcomingRenewals,
    type RenewalRow,
    ONBOARDING_LABEL,
} from "@/lib/cs";

export default function CSPortalPage() {
    const me = useMe();
    const agentDomains = me.data?.user?.agent_domains || [];
    const managerDomains = me.data?.user?.manager_domains || [];
    const isTenantAdmin = me.data?.user?.is_tenant_admin ?? false;
    const allowed =
        isTenantAdmin ||
        agentDomains.includes("customer_service") ||
        managerDomains.includes("customer_service");

    const [days, setDays] = useState(90);
    const { data: renewals = [], isLoading } = useUpcomingRenewals(days);

    if (me.isLoading) return <p className="text-text-muted">Loading…</p>;
    if (!me.data?.user)
        return <p className="text-text-muted">Sign in to view the CS portal.</p>;
    if (!allowed) {
        return (
            <div className="rounded-lg border border-border bg-bg-card p-4">
                <p className="text-text">You don't have CS access.</p>
                <p className="mt-1 text-sm text-text-muted">
                    Ask your tenant admin to add Customer Success to your agent
                    or manager motions under Settings &rarr; User Management.
                </p>
            </div>
        );
    }

    return (
        <div className="space-y-6">
            <header className="flex items-baseline justify-between gap-4">
                <h1 className="text-2xl font-bold">Customer Success</h1>
                <p className="text-sm text-text-muted">
                    Retention, adoption, and expansion across your accounts.
                </p>
            </header>

            <CSAlertsStrip />

            <section>
                <div className="mb-2 flex items-center justify-between">
                    <h2 className="text-sm font-semibold uppercase tracking-wide text-text-muted">
                        Upcoming renewals ({renewals.length})
                    </h2>
                    <label className="text-xs text-text-muted">
                        Window:{" "}
                        <select
                            value={days}
                            onChange={(e) =>
                                setDays(parseInt(e.target.value, 10))
                            }
                            className="ml-1 rounded border border-border bg-bg-card px-2 py-1 text-xs text-text"
                        >
                            <option value={30}>Next 30 days</option>
                            <option value={60}>Next 60 days</option>
                            <option value={90}>Next 90 days</option>
                            <option value={180}>Next 180 days</option>
                        </select>
                    </label>
                </div>
                <div className="rounded-lg border border-border bg-bg-card">
                    {isLoading ? (
                        <p className="p-4 text-sm text-text-muted">Loading…</p>
                    ) : renewals.length === 0 ? (
                        <div className="p-4 text-sm text-text-muted">
                            No renewals in this window. Set renewal dates on
                            accounts in the drill-down page, or wait for the
                            next CRM sync to populate them.
                        </div>
                    ) : (
                        <table className="w-full text-sm">
                            <thead>
                                <tr className="text-left text-xs uppercase tracking-wide text-text-subtle">
                                    <th className="px-3 py-2">Account</th>
                                    <th className="px-3 py-2 text-right">Renewal</th>
                                    <th className="px-3 py-2 text-right">Days</th>
                                    <th className="px-3 py-2 text-right">Health</th>
                                    <th className="px-3 py-2">Onboarding</th>
                                    <th className="px-3 py-2 text-right">Risk</th>
                                </tr>
                            </thead>
                            <tbody>
                                {renewals.map((r) => (
                                    <RenewalRowView key={r.customer_id} r={r} />
                                ))}
                            </tbody>
                        </table>
                    )}
                </div>
            </section>

            <section className="rounded-lg border border-border bg-bg-card p-4">
                <h2 className="text-sm font-semibold uppercase tracking-wide text-text-muted">
                    Recent customer success calls
                </h2>
                <p className="mt-2 text-sm text-text-muted">
                    See every CS interaction filtered to the Customer Success
                    motion in the{" "}
                    <Link
                        href="/interactions?domain=customer_service"
                        className="underline"
                    >
                        interactions inbox
                    </Link>
                    . Each call carries health, adoption, and renewal-risk
                    signals from the CS analyzer.
                </p>
            </section>
        </div>
    );
}

function RenewalRowView({ r }: { r: RenewalRow }) {
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const renewal = new Date(r.renewal_date);
    const daysTo = Math.round(
        (renewal.getTime() - today.getTime()) / 86_400_000,
    );
    const band = riskBand(r.renewal_risk_score);
    const riskClass =
        band === "high"
            ? "bg-error-soft text-error border-error"
            : band === "medium"
            ? "bg-amber-100 text-amber-700 border-amber-300"
            : "bg-emerald-50 text-emerald-700 border-emerald-300";

    return (
        <tr className="border-t border-border hover:bg-bg-card-hover">
            <td className="px-3 py-2">
                <Link
                    href={`/cs/accounts/${r.customer_id}`}
                    className="text-sm font-medium text-text hover:underline"
                >
                    {r.customer_name}
                </Link>
            </td>
            <td className="px-3 py-2 text-right text-sm">
                {renewal.toLocaleDateString()}
            </td>
            <td className="px-3 py-2 text-right text-sm text-text-subtle">
                {daysTo > 0 ? `${daysTo}d` : "Past due"}
            </td>
            <td className="px-3 py-2 text-right text-sm">
                {r.health_score !== null ? r.health_score.toFixed(0) : "—"}
            </td>
            <td className="px-3 py-2 text-sm text-text-muted">
                {r.onboarding_status
                    ? ONBOARDING_LABEL[r.onboarding_status]
                    : "—"}
            </td>
            <td className="px-3 py-2 text-right">
                <span
                    className={`rounded border px-2 py-0.5 text-[10px] font-semibold uppercase ${riskClass}`}
                >
                    {band} · {r.renewal_risk_score.toFixed(0)}
                </span>
            </td>
        </tr>
    );
}

function CSAlertsStrip() {
    const { data: alerts = [] } = useManagerAlerts({
        onlyOpen: true,
        domain: "customer_service",
    });
    if (alerts.length === 0) return null;
    return (
        <section>
            <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-text-muted">
                Manager alerts on your motion ({alerts.length})
            </h2>
            <div className="grid gap-2 md:grid-cols-2">
                {alerts.slice(0, 4).map((a) => (
                    <CSAlertPreview key={a.id} alert={a} />
                ))}
            </div>
        </section>
    );
}

function CSAlertPreview({ alert }: { alert: ManagerAlert }) {
    return (
        <div className="rounded-lg border border-border bg-bg-card p-3">
            <p className="text-sm font-medium text-text">{alert.title}</p>
            {alert.body && <p className="mt-1 text-xs text-text-muted">{alert.body}</p>}
        </div>
    );
}
