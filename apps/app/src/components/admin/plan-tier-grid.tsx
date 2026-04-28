"use client";

import { useMe } from "@/lib/me";
import {
    PlanTierKey,
    TierCatalogEntry,
    useChangeTier,
    useTenantSettings,
} from "@/lib/tenant-settings";
import { humanizeError } from "./section";

const FALLBACK_TIERS: TierCatalogEntry[] = [
    {
        key: "sandbox",
        label: "Sandbox",
        description: "3 seats, 120 min/month. Post-call analysis with Haiku.",
        seat_limit: 3,
        admin_seat_limit: 1,
        max_monthly_minutes: 120,
        max_uploads_per_day: 10,
        ai_model_tier: "haiku",
        features: {},
    },
    {
        key: "starter",
        label: "Starter",
        description: "10 seats, 2k min/month. Adds CRM push + daily CRM sync.",
        seat_limit: 10,
        admin_seat_limit: 1,
        max_monthly_minutes: 2000,
        max_uploads_per_day: null,
        ai_model_tier: "sonnet",
        features: {},
    },
    {
        key: "growth",
        label: "Growth",
        description:
            "50 seats, 10k min/month. Real-time + live coaching + custom scorecards.",
        seat_limit: 50,
        admin_seat_limit: 3,
        max_monthly_minutes: 10000,
        max_uploads_per_day: null,
        ai_model_tier: "sonnet",
        features: {},
    },
    {
        key: "enterprise",
        label: "Enterprise",
        description: "500 seats, 20 admins. Unlimited usage. Full feature set.",
        seat_limit: 500,
        admin_seat_limit: 20,
        max_monthly_minutes: null,
        max_uploads_per_day: null,
        ai_model_tier: "opus",
        features: {},
    },
];

export function PlanTierGrid({ canChange }: { canChange: boolean }) {
    const { data: me } = useMe();
    const { data: settings } = useTenantSettings();
    const change = useChangeTier();

    const catalog = settings?.tier_catalog?.length
        ? settings.tier_catalog
        : FALLBACK_TIERS;
    const currentTier = (me?.tenant.plan_tier as PlanTierKey) ?? "sandbox";

    return (
        <div className="space-y-3">
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
                {catalog.map((tier) => {
                    const isCurrent = tier.key === currentTier;
                    return (
                        <div
                            key={tier.key}
                            className={`rounded-md border p-4 flex flex-col gap-2 ${
                                isCurrent
                                    ? "border-primary bg-primary-soft"
                                    : "border-border bg-bg-raised"
                            }`}
                        >
                            <div className="flex items-baseline justify-between">
                                <span className="font-semibold capitalize">
                                    {tier.label}
                                </span>
                                {isCurrent ? (
                                    <span className="text-xs uppercase tracking-wide text-primary">
                                        Current
                                    </span>
                                ) : null}
                            </div>
                            <p className="text-xs text-text-muted">
                                {tier.description}
                            </p>
                            <ul className="mt-1 space-y-0.5 text-xs text-text-subtle">
                                <li>
                                    Seats:{" "}
                                    <span className="text-text-muted">
                                        {tier.seat_limit}{" "}
                                        ({tier.admin_seat_limit} admin)
                                    </span>
                                </li>
                                <li>
                                    Minutes/mo:{" "}
                                    <span className="text-text-muted">
                                        {tier.max_monthly_minutes ?? "Unlimited"}
                                    </span>
                                </li>
                                <li>
                                    AI tier:{" "}
                                    <span className="text-text-muted capitalize">
                                        {tier.ai_model_tier}
                                    </span>
                                </li>
                            </ul>
                            <button
                                type="button"
                                disabled={
                                    !canChange ||
                                    isCurrent ||
                                    change.isPending
                                }
                                onClick={() =>
                                    change.mutate(tier.key as PlanTierKey)
                                }
                                className={`mt-2 rounded-md px-3 py-1.5 text-xs font-medium transition-colors ${
                                    isCurrent
                                        ? "bg-primary-soft text-primary cursor-default"
                                        : canChange
                                          ? "bg-primary text-white hover:opacity-90"
                                          : "bg-bg-card text-text-subtle cursor-not-allowed"
                                }`}
                            >
                                {isCurrent
                                    ? "Active"
                                    : canChange
                                      ? change.isPending &&
                                        (change.variables as string | undefined) === tier.key
                                          ? "Switching…"
                                          : "Switch"
                                      : "Admin only"}
                            </button>
                        </div>
                    );
                })}
            </div>
            {change.isError ? (
                <p className="text-xs text-accent-rose">
                    {humanizeError(change.error)}
                </p>
            ) : null}
        </div>
    );
}
