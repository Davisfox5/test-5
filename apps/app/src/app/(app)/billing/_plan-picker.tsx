"use client";

/**
 * First-time subscriber plan picker.
 *
 * Three tier cards (Starter / Growth / Enterprise) with a monthly /
 * annual toggle, an inline configurator (seat count, live coaching for
 * Starter only, extra scorecards), a running total, and a CTA that
 * mints a Stripe Checkout Session and full-redirects the browser to
 * Stripe.
 *
 * Pricing catalog is hard-coded here — source of truth is
 * docs/PRICING_MODELS.md and the backend's STRIPE_PRICE_CATALOG env.
 * Any drift between the SPA's display and Stripe's actual line items
 * is cosmetic (Stripe is authoritative on the final amount), but keep
 * them in lockstep when prices change.
 *
 * NOTE: Self-serve is always direct (not partner) pricing. Partner
 * checkout is sales-led; do not surface a partner toggle here.
 */

import { useState } from "react";
import {
    CheckoutCycle,
    CheckoutTier,
    useStripeCheckout,
} from "@/lib/tenant-settings";
import { ErrorCard, humanizeError } from "@/components/admin/section";

interface TierSpec {
    key: CheckoutTier;
    label: string;
    headline: string;
    seats_included: number;
    scorecards_included: string;
    monthly_base: number;
    annual_base: number;
    addl_seat_monthly: number;
    addl_seat_annual: number;
    extra_scorecard_monthly: number;
    extra_scorecard_annual: number;
    onboarding_direct: number;
    bullets: string[];
    sales_led?: boolean;
}

// Live coaching is bundled into Growth + Enterprise; only Starter pays
// for it as an add-on. Numbers from docs/PRICING_MODELS.md.
const LIVE_COACHING_MONTHLY = 25;
const LIVE_COACHING_ANNUAL = 240;

const TIERS: TierSpec[] = [
    {
        key: "starter",
        label: "Starter",
        headline: "Get the basics right",
        seats_included: 10,
        scorecards_included: "1 scorecard",
        monthly_base: 199,
        annual_base: 1909,
        addl_seat_monthly: 25,
        addl_seat_annual: 240,
        extra_scorecard_monthly: 19,
        extra_scorecard_annual: 182,
        onboarding_direct: 399,
        bullets: [
            "Automatic transcription of every call (up to 5,000 minutes per seat, per month)",
            "Speaker separation — see who said what on two-party calls",
            "Full-text transcript search across your last 90 days of calls",
            "AI-generated call summaries so you can skip listening to the full recording",
            "Secure cloud storage of every call recording and transcript",
            "Mobile and web apps for reps and managers",
            "Email support during business hours",
        ],
    },
    {
        key: "growth",
        label: "Growth",
        headline: "Coach smarter, sell more",
        seats_included: 25,
        scorecards_included: "~3 scorecards",
        monthly_base: 999,
        annual_base: 9590,
        addl_seat_monthly: 39,
        addl_seat_annual: 374,
        extra_scorecard_monthly: 39,
        extra_scorecard_annual: 374,
        onboarding_direct: 1499,
        bullets: [
            "Everything in Starter, plus:",
            "AI deep-dive analysis on every call — buying signals, objections, next steps, talk-to-listen ratio, commitments",
            "Automatic PII redaction — credit cards, SSNs, and other sensitive data stripped from transcripts and summaries",
            "Knowledge-Base Q&A — ask \"what did Acme say about pricing last quarter?\" across your entire call history",
            "CRM integrations — two-way sync with Salesforce, HubSpot, or Zoho; auto-log calls, summaries, and action items",
            "Slack notifications — deal-risk alerts and coaching moments pushed to the right channel",
            "Sampled auto-QA scorecards — 10% of calls automatically scored against your rubric",
            "Live AI coaching bundled — real-time suggestions in the rep's ear during live calls",
            "Email + chat support, responses within 1 business day",
        ],
    },
    {
        key: "enterprise",
        label: "Enterprise",
        headline: "Built around your business",
        seats_included: 50,
        scorecards_included: "~5 scorecards (sales-led)",
        monthly_base: 2999,
        annual_base: 28790,
        addl_seat_monthly: 59,
        addl_seat_annual: 566,
        extra_scorecard_monthly: 69,
        extra_scorecard_annual: 795,
        onboarding_direct: 4999,
        sales_led: true,
        bullets: [
            "Everything in Growth, plus:",
            "Dedicated tenant — your own isolated infrastructure, not shared",
            "Custom domain + fully branded UI — your logo, colors, and domain",
            "SSO / SAML / SCIM with Okta, Azure AD, Google Workspace, and any SAML IdP",
            "Bring-your-own AI key — point the platform at your Anthropic, Azure OpenAI, or AWS Bedrock account",
            "Custom AI prompts — our team tunes the analysis and scorecard prompts to your industry",
            "On-prem / VPC transcription option — keep audio inside your network",
            "SOC 2 Type II, HIPAA BAA, GDPR DPA — included",
            "Uptime SLA — 99.9% with service credits",
            "Dedicated Customer Success Manager and business reviews",
            "24/7 support, 1-hour response SLA",
        ],
    },
];

function formatUsd(n: number): string {
    return `$${n.toLocaleString("en-US")}`;
}

interface TotalBreakdown {
    recurring: number;
    onboarding: number;
    first_invoice: number;
    period_label: string;
}

function computeTotals(
    tier: TierSpec,
    cycle: CheckoutCycle,
    addlSeats: number,
    coachingSeats: number,
    extraScorecards: number,
): TotalBreakdown {
    const isAnnual = cycle === "annual";
    const base = isAnnual ? tier.annual_base : tier.monthly_base;
    const seatRate = isAnnual ? tier.addl_seat_annual : tier.addl_seat_monthly;
    const coachRate = isAnnual ? LIVE_COACHING_ANNUAL : LIVE_COACHING_MONTHLY;
    const scRate = isAnnual
        ? tier.extra_scorecard_annual
        : tier.extra_scorecard_monthly;

    // Live coaching only charges on Starter — Growth/Enterprise drop it
    // server-side, so don't double-count it client-side either.
    const coachingApplies = tier.key === "starter" ? coachingSeats : 0;

    const recurring =
        base +
        addlSeats * seatRate +
        coachingApplies * coachRate +
        extraScorecards * scRate;

    return {
        recurring,
        onboarding: tier.onboarding_direct,
        first_invoice: recurring + tier.onboarding_direct,
        period_label: isAnnual ? "/year" : "/mo",
    };
}

export function PlanPicker() {
    const [cycle, setCycle] = useState<CheckoutCycle>("monthly");
    const [openTier, setOpenTier] = useState<CheckoutTier | null>(null);
    const checkout = useStripeCheckout();

    return (
        <div className="space-y-4">
            <div className="flex items-center justify-between gap-4 flex-wrap">
                <p className="text-sm text-text-muted">
                    Pick a plan to get started. You'll land on Stripe Checkout
                    to enter card details and confirm the subscription.
                </p>
                <CycleToggle cycle={cycle} onChange={setCycle} />
            </div>

            <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
                {TIERS.map((tier) => (
                    <TierCard
                        key={tier.key}
                        tier={tier}
                        cycle={cycle}
                        expanded={openTier === tier.key}
                        onToggle={() =>
                            setOpenTier(
                                openTier === tier.key ? null : tier.key,
                            )
                        }
                        checkoutPending={checkout.isPending}
                        onCheckout={async (payload) => {
                            const out = await checkout.mutateAsync(payload);
                            if (out?.url) {
                                // Full navigation — Stripe Checkout is an
                                // external host; SPA navigation can't reach it.
                                window.location.href = out.url;
                            }
                        }}
                    />
                ))}
            </div>

            {checkout.isError ? (
                <ErrorCard
                    message={`Couldn't start checkout: ${humanizeError(checkout.error)}`}
                />
            ) : null}
        </div>
    );
}

function CycleToggle({
    cycle,
    onChange,
}: {
    cycle: CheckoutCycle;
    onChange: (next: CheckoutCycle) => void;
}) {
    return (
        <div
            className="inline-flex rounded-md border border-border bg-bg-raised p-1 text-sm"
            role="group"
            aria-label="Billing cycle"
        >
            <button
                type="button"
                onClick={() => onChange("monthly")}
                className={`rounded px-3 py-1.5 font-medium transition-colors ${
                    cycle === "monthly"
                        ? "bg-bg-card text-text-main shadow-sm"
                        : "text-text-muted hover:text-text-main"
                }`}
            >
                Monthly
            </button>
            <button
                type="button"
                onClick={() => onChange("annual")}
                className={`rounded px-3 py-1.5 font-medium transition-colors ${
                    cycle === "annual"
                        ? "bg-bg-card text-text-main shadow-sm"
                        : "text-text-muted hover:text-text-main"
                }`}
            >
                Annual
                <span className="ml-1.5 inline-block rounded bg-accent-emerald/20 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-accent-emerald">
                    20% off
                </span>
            </button>
        </div>
    );
}

function TierCard({
    tier,
    cycle,
    expanded,
    onToggle,
    onCheckout,
    checkoutPending,
}: {
    tier: TierSpec;
    cycle: CheckoutCycle;
    expanded: boolean;
    onToggle: () => void;
    onCheckout: (payload: {
        tier: CheckoutTier;
        cycle: CheckoutCycle;
        is_partner: boolean;
        addl_seats: number;
        live_coaching_seats: number;
        extra_scorecards: number;
        success_url: string;
        cancel_url: string;
    }) => Promise<void>;
    checkoutPending: boolean;
}) {
    const [addlSeats, setAddlSeats] = useState(0);
    const [coachingSeats, setCoachingSeats] = useState(0);
    const [extraScorecards, setExtraScorecards] = useState(0);

    const headlinePrice =
        cycle === "annual" ? tier.annual_base : tier.monthly_base;
    const totals = computeTotals(
        tier,
        cycle,
        addlSeats,
        coachingSeats,
        extraScorecards,
    );

    const submit = async () => {
        // success_url / cancel_url need to be absolute (Stripe rejects
        // relative URLs), so anchor them to the current origin.
        await onCheckout({
            tier: tier.key,
            cycle,
            // is_partner is always false for self-serve — partner pricing
            // is sales-led and never reachable from this UI.
            is_partner: false,
            addl_seats: addlSeats,
            live_coaching_seats: tier.key === "starter" ? coachingSeats : 0,
            extra_scorecards: extraScorecards,
            success_url: `${window.location.origin}/billing?status=success`,
            cancel_url: `${window.location.origin}/billing?status=cancel`,
        });
    };

    return (
        <div className="rounded-lg border border-border bg-bg-raised p-5 flex flex-col gap-3">
            <div>
                <div className="flex items-baseline justify-between gap-2">
                    <h4 className="text-lg font-semibold">{tier.label}</h4>
                    {tier.sales_led ? (
                        <span className="text-[10px] uppercase tracking-wide text-text-subtle">
                            Sales-led
                        </span>
                    ) : null}
                </div>
                <p className="text-xs text-text-muted">{tier.headline}</p>
            </div>

            <div>
                <div className="text-2xl font-bold">
                    {formatUsd(headlinePrice)}
                    <span className="text-sm font-normal text-text-muted">
                        {cycle === "annual" ? "/year" : "/mo"}
                    </span>
                </div>
                <p className="text-xs text-text-subtle">
                    up to {tier.seats_included} seats included
                    {" · "}
                    {tier.scorecards_included}
                </p>
                <p className="mt-1 text-xs text-text-subtle">
                    One-time onboarding: {formatUsd(tier.onboarding_direct)}
                </p>
            </div>

            <ul className="space-y-1 text-xs text-text-muted">
                {tier.bullets.map((b, i) => (
                    <li
                        key={i}
                        className={
                            b.endsWith(":")
                                ? "font-semibold text-text-main"
                                : "pl-3 -indent-3 before:content-['•'] before:mr-1.5 before:text-text-subtle"
                        }
                    >
                        {b}
                    </li>
                ))}
            </ul>

            <div className="mt-auto pt-2">
                <button
                    type="button"
                    onClick={onToggle}
                    className="w-full rounded-md bg-primary px-3 py-2 text-sm font-medium text-white hover:opacity-90"
                >
                    {expanded ? "Hide options" : `Choose ${tier.label}`}
                </button>
            </div>

            {expanded ? (
                <div className="rounded-md border border-border bg-bg-card p-3 space-y-3 text-sm">
                    <NumberField
                        label="Additional seats"
                        helper={`Includes ${tier.seats_included} seats; add'l at ${formatUsd(
                            cycle === "annual"
                                ? tier.addl_seat_annual
                                : tier.addl_seat_monthly,
                        )}${cycle === "annual" ? "/year" : "/mo"} per seat`}
                        value={addlSeats}
                        onChange={setAddlSeats}
                    />
                    {tier.key === "starter" ? (
                        <NumberField
                            label="Live AI Coaching seats"
                            helper={`${formatUsd(
                                cycle === "annual"
                                    ? LIVE_COACHING_ANNUAL
                                    : LIVE_COACHING_MONTHLY,
                            )}${cycle === "annual" ? "/year" : "/mo"} per seat — bundled in Growth and Enterprise`}
                            value={coachingSeats}
                            onChange={setCoachingSeats}
                        />
                    ) : null}
                    <NumberField
                        label="Extra scorecards"
                        helper={`${formatUsd(
                            cycle === "annual"
                                ? tier.extra_scorecard_annual
                                : tier.extra_scorecard_monthly,
                        )}${cycle === "annual" ? "/year" : "/mo"} each`}
                        value={extraScorecards}
                        onChange={setExtraScorecards}
                    />

                    <div className="rounded-md bg-bg-raised p-3 text-xs space-y-1">
                        <div className="flex items-center justify-between">
                            <span className="text-text-subtle">
                                Subscription
                            </span>
                            <span className="font-semibold text-text-main">
                                {formatUsd(totals.recurring)}
                                {totals.period_label}
                            </span>
                        </div>
                        <div className="flex items-center justify-between">
                            <span className="text-text-subtle">
                                First invoice
                            </span>
                            <span className="font-semibold text-text-main">
                                {formatUsd(totals.first_invoice)}
                            </span>
                        </div>
                        <p className="text-text-subtle">
                            Includes one-time {formatUsd(totals.onboarding)}{" "}
                            onboarding fee.
                        </p>
                    </div>

                    <button
                        type="button"
                        onClick={submit}
                        disabled={checkoutPending}
                        className="w-full rounded-md bg-primary px-3 py-2 text-sm font-medium text-white hover:opacity-90 disabled:opacity-50"
                    >
                        {checkoutPending
                            ? "Redirecting to Stripe…"
                            : `Continue to checkout — ${formatUsd(totals.first_invoice)}`}
                    </button>
                </div>
            ) : null}
        </div>
    );
}

function NumberField({
    label,
    helper,
    value,
    onChange,
}: {
    label: string;
    helper: string;
    value: number;
    onChange: (next: number) => void;
}) {
    return (
        <label className="block">
            <span className="block text-xs font-medium text-text-main">
                {label}
            </span>
            <input
                type="number"
                min={0}
                step={1}
                value={value}
                onChange={(e) => {
                    const parsed = Number.parseInt(e.target.value, 10);
                    onChange(Number.isFinite(parsed) && parsed >= 0 ? parsed : 0);
                }}
                className="mt-1 w-24 rounded-md border border-border bg-bg-raised px-2 py-1 text-sm focus:border-primary focus:outline-none"
            />
            <span className="mt-1 block text-[11px] text-text-subtle">
                {helper}
            </span>
        </label>
    );
}
