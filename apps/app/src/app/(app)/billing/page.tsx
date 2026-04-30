"use client";

import { useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useMe } from "@/lib/me";
import { useStripePortalLink } from "@/lib/tenant-settings";
import {
    AdminGate,
    ErrorCard,
    Section,
    humanizeError,
} from "@/components/admin/section";
import { PlanTierGrid } from "@/components/admin/plan-tier-grid";
import { PlanPicker } from "./_plan-picker";

export default function BillingPage() {
    const { data: me } = useMe();
    const role = me?.user?.role;
    const isAdmin = role === "admin";
    const portal = useStripePortalLink();
    const hasSubscription = me?.tenant.has_subscription ?? false;

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
                    {hasSubscription
                        ? `Manage the subscription for ${me?.tenant.name ?? "your tenant"} via Stripe.`
                        : `Pick a tier for ${me?.tenant.name ?? "your tenant"} or jump into Stripe to update payment details.`}
                </p>
            </header>

            <CheckoutStatusBanner />

            <AdminGate role={role}>
                {!hasSubscription ? (
                    <Section
                        title="Subscribe"
                        subtitle="Choose a plan, configure seats and add-ons, then continue to Stripe Checkout."
                    >
                        <PlanPicker />
                    </Section>
                ) : null}

                <Section
                    title="Choose a plan"
                    subtitle="Switching tiers updates seat caps and feature flags immediately. Downgrades trigger seat reconciliation."
                >
                    <PlanTierGrid canChange={isAdmin} />
                </Section>

                {hasSubscription ? (
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
                ) : null}

                <Section
                    title="Trial status"
                    subtitle="Sandbox tenants get a free trial; switch to a paid tier before it expires."
                >
                    <ul className="space-y-1 text-sm">
                        <li>
                            <span className="text-text-subtle">
                                Current tier:
                            </span>{" "}
                            <span className="font-medium">
                                {/* `plan_tier` already arrives lower-case
                                    from the backend (`sandbox`, etc.); cap
                                    only the first letter rather than using
                                    `text-transform: capitalize`, which
                                    would title-case a (hypothetical)
                                    multi-word tier and read as "Sandbox
                                    Plus" instead of "Sandbox plus". */}
                                {me?.tenant.plan_tier
                                    ? me.tenant.plan_tier.charAt(0).toUpperCase() +
                                      me.tenant.plan_tier.slice(1)
                                    : "—"}
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

/**
 * Reads ``?status=`` set by Stripe Checkout's success / cancel
 * redirect URLs. Surfaces a banner so the user gets feedback on what
 * happened before any /me refetch settles. Self-dismissing — the
 * banner stays until the user clicks the close button.
 */
function CheckoutStatusBanner() {
    const params = useSearchParams();
    const [dismissed, setDismissed] = useState(false);
    // Snapshot the param at mount so the banner doesn't blink away if
    // the user happens to navigate while it's open.
    const [status, setStatus] = useState<string | null>(null);
    useEffect(() => {
        const raw = params?.get("status");
        if (raw === "success" || raw === "cancel") {
            setStatus(raw);
        }
    }, [params]);

    if (dismissed || !status) return null;

    const isSuccess = status === "success";
    return (
        <div
            className={`flex items-start justify-between gap-3 rounded-md border p-3 text-sm ${
                isSuccess
                    ? "border-accent-emerald/40 bg-accent-emerald/10 text-text-main"
                    : "border-accent-amber/40 bg-accent-amber/10 text-text-main"
            }`}
        >
            <div>
                <p className="font-medium">
                    {isSuccess
                        ? "Subscription started"
                        : "Checkout cancelled"}
                </p>
                <p className="text-xs text-text-muted">
                    {isSuccess
                        ? "Stripe is processing the first invoice. Your plan will update within a few seconds."
                        : "No charge was made. You can try again whenever you're ready."}
                </p>
            </div>
            <button
                type="button"
                onClick={() => setDismissed(true)}
                aria-label="Dismiss"
                className="text-text-subtle hover:text-text-main"
            >
                ×
            </button>
        </div>
    );
}
