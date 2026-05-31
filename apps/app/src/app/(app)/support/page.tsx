"use client";

/**
 * IT Support agent portal.
 *
 * Renders the Support-motion agent shell: a case-queue summary, an
 * escalations strip, and a recent-interaction list filtered to
 * ``Interaction.domain === 'it_support'``.
 *
 * Gated on the signed-in user's ``agent_domains`` including
 * ``it_support``. Tenant admins always pass.
 */

import Link from "next/link";
import { useMe } from "@/lib/me";
import { useManagerAlerts, type ManagerAlert } from "@/lib/manager";

export default function SupportPortalPage() {
    const me = useMe();
    const agentDomains = me.data?.user?.agent_domains || [];
    const isTenantAdmin = me.data?.user?.is_tenant_admin ?? false;
    const allowed = isTenantAdmin || agentDomains.includes("it_support");

    if (me.isLoading) return <p className="text-text-muted">Loading…</p>;
    if (!me.data?.user) {
        return (
            <p className="text-text-muted">Sign in to view the support portal.</p>
        );
    }
    if (!allowed) {
        return (
            <div className="rounded-lg border border-border bg-bg-card p-4">
                <p className="text-text">You don't have IT Support agent access.</p>
                <p className="mt-1 text-sm text-text-muted">
                    Ask your tenant admin to add IT Support to your agent
                    motions under Settings → User Management.
                </p>
            </div>
        );
    }

    return (
        <div className="space-y-6">
            <header className="flex items-baseline justify-between gap-4">
                <h1 className="text-2xl font-bold">IT Support</h1>
                <p className="text-sm text-text-muted">
                    Cases, escalations, and CSAT for your queue.
                </p>
            </header>

            <SupportAlertsStrip />
            <CaseQueueCard />
            <RecentSupportInteractionsCard />
        </div>
    );
}

function SupportAlertsStrip() {
    const { data: alerts = [] } = useManagerAlerts({
        onlyOpen: true,
        domain: "it_support",
    });
    if (alerts.length === 0) return null;
    return (
        <section>
            <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-text-muted">
                Manager alerts on your motion ({alerts.length})
            </h2>
            <div className="grid gap-2 md:grid-cols-2">
                {alerts.slice(0, 4).map((a) => (
                    <SupportAlertPreview key={a.id} alert={a} />
                ))}
            </div>
        </section>
    );
}

function SupportAlertPreview({ alert }: { alert: ManagerAlert }) {
    return (
        <div className="rounded-lg border border-border bg-bg-card p-3">
            <p className="text-sm font-medium text-text">{alert.title}</p>
            {alert.body && (
                <p className="mt-1 text-xs text-text-muted">{alert.body}</p>
            )}
        </div>
    );
}

function CaseQueueCard() {
    // Case-queue table will read from a future ``/support/cases`` endpoint.
    // The model is in place (``SupportCase``); the first cut shows the
    // section's placement and points to the inbox.
    return (
        <section>
            <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-text-muted">
                Case queue
            </h2>
            <div className="rounded-lg border border-border bg-bg-card p-4">
                <p className="text-sm text-text-muted">
                    Cases group every call and email belonging to one customer
                    issue from open through close. The queue view will list
                    your open and escalated cases here once cases start being
                    created from inbound interactions.
                </p>
            </div>
        </section>
    );
}

function RecentSupportInteractionsCard() {
    return (
        <section>
            <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-text-muted">
                Recent support interactions
            </h2>
            <div className="rounded-lg border border-border bg-bg-card p-4">
                <p className="text-sm text-text-muted">
                    See every support call and email in the{" "}
                    <Link
                        href="/interactions?domain=it_support"
                        className="underline"
                    >
                        interactions inbox
                    </Link>
                    , filtered to the IT Support motion. Each one carries
                    resolution, escalation, and CSAT signals from the Support
                    analyzer.
                </p>
            </div>
        </section>
    );
}
