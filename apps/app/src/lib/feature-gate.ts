"use client";

import type { PlanLimits } from "./me";
import { useMe } from "./me";

type FeatureFlag = {
    [K in keyof PlanLimits]: PlanLimits[K] extends boolean ? K : never;
}[keyof PlanLimits];

/**
 * useFeature("live_coaching") — returns { enabled, loading, reason }.
 * The server still enforces the gate via require_feature(); this hook
 * just shapes the UI so users aren't offered buttons that will 402.
 */
export function useFeature(flag: FeatureFlag) {
    const { data, isLoading } = useMe();
    if (isLoading || !data) return { enabled: false, loading: true, reason: null as string | null };
    if (data.tenant.trial_expired) {
        return { enabled: false, loading: false, reason: "Your sandbox trial has ended." };
    }
    const enabled = Boolean(data.tenant.limits[flag]);
    return {
        enabled,
        loading: false,
        reason: enabled ? null : `Not available on the ${data.tenant.plan_tier} plan.`,
    };
}

export function useRole() {
    const { data } = useMe();
    return data?.user?.role ?? null;
}
