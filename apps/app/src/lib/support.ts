"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useApi } from "./api";

export type CaseStatus =
    | "open"
    | "in_progress"
    | "escalated"
    | "resolved"
    | "closed";

export type CasePriority = "high" | "medium" | "low";

export interface SupportCaseSummary {
    id: string;
    subject: string;
    status: CaseStatus;
    priority: CasePriority;
    assigned_to: string | null;
    assigned_to_name: string | null;
    customer_id: string | null;
    customer_name: string | null;
    opened_at: string;
    first_response_at: string | null;
    escalated_at: string | null;
    resolved_at: string | null;
    closed_at: string | null;
    csat_score: number | null;
    first_contact_resolution: boolean | null;
    interaction_count: number;
}

export interface SupportCaseInteractionRow {
    id: string;
    channel: string;
    direction: string | null;
    title: string | null;
    created_at: string;
}

export interface SupportCaseDetail extends SupportCaseSummary {
    description: string | null;
    category: string | null;
    interactions: SupportCaseInteractionRow[];
    metadata: Record<string, unknown>;
}

export interface ListCasesParams {
    status?: CaseStatus | "open_all";
    mineOnly?: boolean;
    priority?: CasePriority;
    limit?: number;
}

export function useSupportCases(params: ListCasesParams = {}) {
    const api = useApi();
    return useQuery({
        queryKey: ["support", "cases", params],
        queryFn: () => {
            const qp = new URLSearchParams();
            if (params.status) qp.set("status", params.status);
            if (params.mineOnly) qp.set("mine_only", "true");
            if (params.priority) qp.set("priority", params.priority);
            if (params.limit) qp.set("limit", String(params.limit));
            return api.get<SupportCaseSummary[]>(
                `/support/cases${qp.toString() ? `?${qp}` : ""}`,
            );
        },
        refetchInterval: 30_000,
    });
}

export function useSupportCase(caseId: string | null | undefined) {
    const api = useApi();
    return useQuery({
        queryKey: ["support", "case", caseId],
        queryFn: () => api.get<SupportCaseDetail>(`/support/cases/${caseId}`),
        enabled: !!caseId,
    });
}

export function useCreateSupportCase() {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: (payload: {
            subject: string;
            description?: string;
            priority?: CasePriority;
            customer_id?: string;
            category?: string;
        }) => api.post<SupportCaseDetail>("/support/cases", payload),
        onSuccess: () => {
            qc.invalidateQueries({ queryKey: ["support", "cases"] });
        },
    });
}

export function useTransitionCase() {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: ({
            caseId,
            status,
        }: {
            caseId: string;
            status: CaseStatus;
        }) =>
            api.post<SupportCaseDetail>(
                `/support/cases/${caseId}/status`,
                { status },
            ),
        onSuccess: (_data, vars) => {
            qc.invalidateQueries({ queryKey: ["support", "case", vars.caseId] });
            qc.invalidateQueries({ queryKey: ["support", "cases"] });
        },
    });
}

export function useAssignCase() {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: ({
            caseId,
            userId,
        }: {
            caseId: string;
            userId: string | null;
        }) =>
            api.post<SupportCaseDetail>(`/support/cases/${caseId}/assign`, {
                user_id: userId,
            }),
        onSuccess: (_data, vars) => {
            qc.invalidateQueries({ queryKey: ["support", "case", vars.caseId] });
            qc.invalidateQueries({ queryKey: ["support", "cases"] });
        },
    });
}

export function useSetPriority() {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: ({
            caseId,
            priority,
        }: {
            caseId: string;
            priority: CasePriority;
        }) =>
            api.post<SupportCaseDetail>(`/support/cases/${caseId}/priority`, {
                priority,
            }),
        onSuccess: (_data, vars) => {
            qc.invalidateQueries({ queryKey: ["support", "case", vars.caseId] });
            qc.invalidateQueries({ queryKey: ["support", "cases"] });
        },
    });
}

export function useRecordCsat() {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: ({
            caseId,
            score,
        }: {
            caseId: string;
            score: number;
        }) =>
            api.post<SupportCaseDetail>(`/support/cases/${caseId}/csat`, {
                score,
            }),
        onSuccess: (_data, vars) => {
            qc.invalidateQueries({ queryKey: ["support", "case", vars.caseId] });
            qc.invalidateQueries({ queryKey: ["support", "cases"] });
        },
    });
}

export interface CsatToken {
    token: string;
    public_url: string;
}

export function useIssueCsatToken() {
    const api = useApi();
    return useMutation({
        mutationFn: (caseId: string) =>
            api.post<CsatToken>(`/support/cases/${caseId}/csat-token`),
    });
}

// ── Vocabulary helpers ────────────────────────────────────────────────

export const STATUS_LABEL: Record<CaseStatus, string> = {
    open: "Open",
    in_progress: "In progress",
    escalated: "Escalated",
    resolved: "Resolved",
    closed: "Closed",
};

export const PRIORITY_LABEL: Record<CasePriority, string> = {
    high: "High",
    medium: "Medium",
    low: "Low",
};

export const STATUS_COLORS: Record<CaseStatus, string> = {
    open: "bg-blue-50 text-blue-700 border-blue-200",
    in_progress: "bg-amber-100 text-amber-700 border-amber-300",
    escalated: "bg-error-soft text-error border-error",
    resolved: "bg-emerald-50 text-emerald-700 border-emerald-300",
    closed: "bg-slate-100 text-slate-600 border-slate-300",
};
