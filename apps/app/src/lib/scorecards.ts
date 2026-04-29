"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useApi } from "./api";

export interface ScorecardCriterion {
    name: string;
    weight: number;
    description?: string;
    // The IRT fitter writes back a/b/passing_rate; surface them as
    // optional read-only fields so the editor can preserve them on PUT.
    a?: number;
    b?: number;
    passing_rate?: number;
    [key: string]: unknown;
}

export interface ScorecardTemplate {
    id: string;
    tenant_id: string;
    name: string;
    criteria: ScorecardCriterion[];
    channel_filter: string[] | null;
    is_default: boolean;
    created_at: string;
}

export interface ScorecardCreate {
    name: string;
    criteria: ScorecardCriterion[];
    channel_filter?: string[] | null;
    is_default?: boolean;
}

export interface ScorecardUpdate {
    name?: string;
    criteria?: ScorecardCriterion[];
    channel_filter?: string[] | null;
    is_default?: boolean;
}

export function useScorecards() {
    const api = useApi();
    return useQuery({
        queryKey: ["scorecards"],
        queryFn: () => api.get<ScorecardTemplate[]>("/scorecards"),
        staleTime: 30_000,
    });
}

export function useScorecard(id: string | undefined) {
    const api = useApi();
    return useQuery({
        queryKey: ["scorecard", id],
        // Hits GET /scorecards/{id} directly — the previous implementation
        // pulled the full list and filtered client-side, which was O(N)
        // per detail render once tenants accumulate many templates.
        queryFn: () => api.get<ScorecardTemplate>(`/scorecards/${id}`),
        enabled: Boolean(id),
    });
}

export function useCreateScorecard() {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: (body: ScorecardCreate) =>
            api.post<ScorecardTemplate>("/scorecards", body),
        onSuccess: () => {
            qc.invalidateQueries({ queryKey: ["scorecards"] });
        },
    });
}

export function useUpdateScorecard() {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: ({ id, patch }: { id: string; patch: ScorecardUpdate }) =>
            api.request<ScorecardTemplate>(`/scorecards/${id}`, {
                method: "PUT",
                body: JSON.stringify(patch),
            }),
        onSuccess: (data) => {
            qc.invalidateQueries({ queryKey: ["scorecards"] });
            qc.setQueryData(["scorecard", data.id], data);
        },
    });
}

export function useDeleteScorecard() {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: (id: string) => api.del<void>(`/scorecards/${id}`),
        onMutate: async (id) => {
            await qc.cancelQueries({ queryKey: ["scorecards"] });
            const previous = qc.getQueryData<ScorecardTemplate[]>(["scorecards"]);
            if (previous) {
                qc.setQueryData<ScorecardTemplate[]>(
                    ["scorecards"],
                    previous.filter((t) => t.id !== id),
                );
            }
            return { previous };
        },
        onError: (_err, _id, ctx) => {
            if (ctx?.previous) qc.setQueryData(["scorecards"], ctx.previous);
        },
        onSettled: () => {
            qc.invalidateQueries({ queryKey: ["scorecards"] });
        },
    });
}

export function totalWeight(criteria: ScorecardCriterion[]): number {
    return criteria.reduce((sum, c) => sum + (Number(c.weight) || 0), 0);
}

export interface ScorecardValidation {
    ok: boolean;
    errors: string[];
}

export function validateScorecard(
    name: string,
    criteria: ScorecardCriterion[],
): ScorecardValidation {
    const errors: string[] = [];
    if (!name.trim()) errors.push("Name is required.");
    if (criteria.length === 0) {
        errors.push("Add at least one rubric item.");
    }
    if (criteria.some((c) => !c.name.trim())) {
        errors.push("Every rubric item needs a name.");
    }
    if (criteria.some((c) => !Number.isFinite(c.weight) || c.weight < 0)) {
        errors.push("Weights must be non-negative numbers.");
    }
    if (criteria.length > 0 && totalWeight(criteria) !== 100) {
        errors.push(`Weights must sum to 100 (currently ${totalWeight(criteria)}).`);
    }
    return { ok: errors.length === 0, errors };
}
