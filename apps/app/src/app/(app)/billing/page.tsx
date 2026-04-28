"use client";

import { useMe } from "@/lib/me";
import { useStripePortalLink } from "@/lib/tenant-settings";
import {
    AdminGate,
    ErrorCard,
    Section,
    humanizeError,
} from "@/components/admin/section";
import { PlanTierGrid } from "@/components/admin/plan-tier-grid";

export default function BillingPage() {
    const { data: me } = useMe();
    const role = me?.user?.role;
    const isAdmin = role === "admin";
    const portal = useStripePortalLink();

    const onManage = async () => {
        try {
            const out = await portal.mutateAsync();
            if (out?.url) {
                window.open(out.url, "_blank", "noopener,noreferrer");
            }
        } catch {
            // Surfaced inline below; backend may not have the endpoint
            // wired yet (returns 404), which is fine — admins can still
            // change tiers from the cards above.
        }
    };

    return (
        <div className="space-y-6">
            <header>
                <h2 className="text-2xl font-bold">Billing & plan</h2>
                <p className="text-text-muted mt-1">
                    Pick a tier for {me?.tenant.name ?? "your tenant"} or jump
                    into Stripe to update payment details.
                </p>
            </header>

            <AdminGate role={role}>
                <Section
                    title="Choose a plan"
                    subtitle="Switching tiers updates seat caps and feature flags immediately. Downgrades trigger seat reconciliation."
                >
                    <PlanTierGrid canChange={isAdmin} />
                </Section>

                <Section
                    title="Stripe"
                    subtitle="Open the Stripe customer portal to update card on file, view invoices, or cancel."
                >
                    <button
                        type="button"
                        onClick={onManage}
                        disabled={portal.isPending}
                        className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-white disabled:opacity-50"
                    >
                        {portal.isPending
                            ? "Generating link…"
                            : "Manage Stripe subscription"}
                    </button>
                    {portal.isError ? (
                        <div className="mt-3">
                            <ErrorCard
                                message={`Stripe portal not available: ${humanizeError(portal.error)}`}
                            />
                        </div>
                    ) : null}
                </Section>

                <Section
                    title="Trial status"
                    subtitle="Sandbox tenants get a free trial; switch to a paid tier before it expires."
                >
                    <ul className="space-y-1 text-sm">
                        <li>
                            <span className="text-text-subtle">
                                Current tier:
                            </span>{" "}
                            <span className="font-medium capitalize">
                                {me?.tenant.plan_tier ?? "—"}
                            </span>
                        </li>
                        <li>
                            <span className="text-text-subtle">
                                Trial active:
                            </span>{" "}
                            {me?.tenant.trial_active ? (
                                <span className="text-accent-emerald">
                                    Yes
                                </span>
                            ) : (
                                <span className="text-text-muted">No</span>
                            )}
                        </li>
                        {me?.tenant.trial_ends_at ? (
                            <li>
                                <span className="text-text-subtle">
                                    Trial ends:
                                </span>{" "}
                                {new Date(
                                    me.tenant.trial_ends_at,
                                ).toLocaleString()}
                            </li>
                        ) : null}
                    </ul>
                </Section>
            </AdminGate>
        </div>
    );
}
