"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useApi } from "./api";
import type { Domain } from "./me";

export type ConcernStatus = "active" | "monitoring" | "resolved" | "dormant";
export type ConcernSeverity = "high" | "medium" | "low";
export type CommitmentStatus = "open" | "met" | "broken" | "dismissed";

export interface Concern {
    id: string;
    topic: string;
    description: string | null;
    status: ConcernStatus;
    severity: ConcernSeverity;
    source_motion: Domain | null;
    first_seen_at: string;
    last_seen_at: string;
    resolved_at: string | null;
    status_changed_at: string;
    evidence_count: number;
}

export interface Commitment {
    id: string;
    description: string;
    quote: string | null;
    due_date: string | null;
    status: CommitmentStatus;
    met_at: string | null;
    source_interaction_id: string | null;
    created_at: string;
}

export interface CustomerMemory {
    customer_id: string;
    customer_name: string;
    concerns: Concern[];
    commitments: Commitment[];
}

export function useCustomerMemory(customerId: string | null | undefined) {
    const api = useApi();
    return useQuery({
        queryKey: ["customer-memory", customerId],
        queryFn: () =>
            api.get<CustomerMemory>(`/customers/${customerId}/memory`),
        enabled: !!customerId,
    });
}

export function usePatchConcern(customerId: string) {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: ({
            id,
            patch,
        }: {
            id: string;
            patch: Partial<Pick<Concern, "status" | "severity" | "description">>;
        }) =>
            api.patch<Concern>(
                `/customers/${customerId}/concerns/${id}`,
                patch,
            ),
        onSuccess: () => {
            qc.invalidateQueries({ queryKey: ["customer-memory", customerId] });
        },
    });
}

export function usePatchCommitment(customerId: string) {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: ({
            id,
            patch,
        }: {
            id: string;
            patch: Partial<
                Pick<Commitment, "status" | "description" | "due_date">
            >;
        }) =>
            api.patch<Commitment>(
                `/customers/${customerId}/commitments/${id}`,
                patch,
            ),
        onSuccess: () => {
            qc.invalidateQueries({ queryKey: ["customer-memory", customerId] });
        },
    });
}

export const STATUS_LABEL: Record<ConcernStatus, string> = {
    active: "Active",
    monitoring: "Monitoring",
    resolved: "Resolved",
    dormant: "Dormant",
};

export const STATUS_COLORS: Record<ConcernStatus, string> = {
    active: "bg-error-soft text-error border-error",
    monitoring: "bg-amber-100 text-amber-700 border-amber-300",
    resolved: "bg-emerald-50 text-emerald-700 border-emerald-300",
    dormant: "bg-slate-100 text-slate-600 border-slate-300",
};

export const COMMITMENT_STATUS_LABEL: Record<CommitmentStatus, string> = {
    open: "Open",
    met: "Met",
    broken: "Broken",
    dismissed: "Dismissed",
};
