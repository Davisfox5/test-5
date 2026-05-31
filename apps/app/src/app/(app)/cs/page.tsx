"use client";

/**
 * Customer Success agent portal.
 *
 * Renders the CS-motion agent shell: an account-health timeline drawn
 * from CS interactions, an at-risk-accounts strip, and a recent-call
 * list filtered to ``Interaction.domain === 'customer_service'``.
 *
 * Gated on the signed-in user's ``agent_domains`` including
 * ``customer_service``. Tenant admins always pass.
 */

import Link from "next/link";
import { useMe } from "@/lib/me";
import { useManagerAlerts, type ManagerAlert } from "@/lib/manager";

export default function CSPortalPage() {
    const me = useMe();
    const agentDomains = me.data?.user?.agent_domains || [];
    const isTenantAdmin = me.data?.user?.is_tenant_admin ?? false;
    const allowed = isTenantAdmin || agentDomains.includes("customer_service");

    if (me.isLoading) return <p className="text-text-muted">Loading…</p>;
    if (!me.data?.user) {
        return <p className="text-text-muted">Sign in to view the CS portal.</p>;
    }
    if (!allowed) {
        return (
            <div className="rounded-lg border border-border bg-bg-card p-4">
                <p className="text-text">You don't have CS agent access.</p>
                <p className="mt-1 text-sm text-text-muted">
                    Ask your tenant admin to add Customer Success to your agent
                    motions under Settings → User Management.
                </p>
            </div>
        );
    }

    return (
        <div className="space-y-6">
            <header className="flex items-baseline justify-between gap-4">
                <h1 className="text-2xl font-bold">Customer Success</h1>
                <p className="text-sm text-text-muted">
                    Retention, adoption, and expansion for your accounts.
                </p>
            </header>

            <CSAlertsStrip />
            <UpcomingRenewalsCard />
            <RecentCSInteractionsCard />
        </div>
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
            {alert.body && (
                <p className="mt-1 text-xs text-text-muted">{alert.body}</p>
            )}
        </div>
    );
}

function UpcomingRenewalsCard() {
    // The renewal list will read from a future ``/cs/renewals`` endpoint
    // (Customer model has a ``renewal_date`` extension on tenant_context).
    // For the first cut we render the empty state so the section's
    // place is visible without inventing data.
    return (
        <section>
            <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-text-muted">
                Upcoming renewals
            </h2>
            <div className="rounded-lg border border-border bg-bg-card p-4">
                <p className="text-sm text-text-muted">
                    Renewal tracking will populate once your accounts have a
                    renewal date attached. Connect your CRM under{" "}
                    <Link href="/settings/integrations" className="underline">
                        Settings &rarr; Integrations
                    </Link>{" "}
                    to pull dates in automatically.
                </p>
            </div>
        </section>
    );
}

function RecentCSInteractionsCard() {
    return (
        <section>
            <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-text-muted">
                Recent customer success calls
            </h2>
            <div className="rounded-lg border border-border bg-bg-card p-4">
                <p className="text-sm text-text-muted">
                    See every CS interaction in the{" "}
                    <Link
                        href="/interactions?domain=customer_service"
                        className="underline"
                    >
                        interactions inbox
                    </Link>
                    , filtered to the Customer Success motion. Each call carries
                    health, adoption, and renewal-risk signals from the CS
                    analyzer.
                </p>
            </div>
        </section>
    );
}
