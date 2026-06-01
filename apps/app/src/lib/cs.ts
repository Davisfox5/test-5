"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useApi } from "./api";

export type OnboardingStatus =
    | "not_started"
    | "in_progress"
    | "stalled"
    | "completed";

export interface RenewalRow {
    customer_id: string;
    customer_name: string;
    renewal_date: string;
    health_score: number | null;
    onboarding_status: OnboardingStatus | null;
    renewal_risk_score: number;
}

export interface HealthBreakdown {
    engagement: number;
    sentiment: number;
    churn_signal: number;
    onboarding: number;
    renewal_proximity: number;
    overall: number;
    cs_interaction_count: number;
    last_cs_at: string | null;
}

export interface AccountDetail {
    customer_id: string;
    customer_name: string;
    renewal_date: string | null;
    onboarding_status: OnboardingStatus | null;
    health_score: number | null;
    health_breakdown: HealthBreakdown;
    renewal_risk_score: number;
}

export function useUpcomingRenewals(daysAhead: number = 90) {
    const api = useApi();
    return useQuery({
        queryKey: ["cs", "renewals", daysAhead],
        queryFn: () =>
            api.get<RenewalRow[]>(`/cs/renewals?days_ahead=${daysAhead}`),
        refetchOnWindowFocus: false,
    });
}

export function useAccountHealth(
    customerId: string | null | undefined,
    opts: { recompute?: boolean } = {},
) {
    const api = useApi();
    return useQuery({
        queryKey: ["cs", "account", customerId, { recompute: !!opts.recompute }],
        queryFn: () =>
            api.get<AccountDetail>(
                `/cs/accounts/${customerId}/health${opts.recompute ? "?recompute=true" : ""}`,
            ),
        enabled: !!customerId,
    });
}

export function usePatchCustomerCsFields() {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: ({
            customerId,
            patch,
        }: {
            customerId: string;
            patch: {
                renewal_date?: string | null;
                onboarding_status?: OnboardingStatus;
            };
        }) =>
            api.patch<AccountDetail>(
                `/cs/accounts/${customerId}`,
                patch,
            ),
        onSuccess: (_data, vars) => {
            qc.invalidateQueries({ queryKey: ["cs", "account", vars.customerId] });
            qc.invalidateQueries({ queryKey: ["cs", "renewals"] });
        },
    });
}

export const ONBOARDING_LABEL: Record<OnboardingStatus, string> = {
    not_started: "Not started",
    in_progress: "In progress",
    stalled: "Stalled",
    completed: "Completed",
};

export function riskBand(score: number): "high" | "medium" | "low" {
    if (score >= 70) return "high";
    if (score >= 40) return "medium";
    return "low";
}
