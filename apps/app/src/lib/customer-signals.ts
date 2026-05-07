"use client";

import { useQuery } from "@tanstack/react-query";
import { useApi } from "./api";

export interface BehaviorRadarValues {
    commitment: number;
    openness: number;
    engagement: number;
    trust: number;
    decision_urgency: number;
    friction: number;
}

export interface ChangeReadinessOutput {
    score: number;
    confidence: "low" | "medium" | "high" | string;
    contributing: Record<string, number>;
}

export interface CustomerBehaviorSignals {
    customer_id: string;
    radar: BehaviorRadarValues;
    change_readiness: ChangeReadinessOutput;
    signal_density: number;
    source_interaction_count: number;
}

export function useCustomerBehaviorSignals(customerId: string | undefined) {
    const api = useApi();
    return useQuery({
        queryKey: ["customer-behavior-signals", customerId],
        queryFn: () =>
            api.get<CustomerBehaviorSignals>(
                `/customers/${customerId}/behavior-signals`,
            ),
        enabled: Boolean(customerId),
    });
}
