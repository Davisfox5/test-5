"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useApi } from "./api";

// ── Types ─────────────────────────────────────────────────────────────

export interface ManagerNarrative {
    as_of: string | null;
    summary: string;
    top_factors: unknown[];
    confidence: number | null;
    version: number;
    playbook_insights: Record<string, unknown>;
}

export type AlertKind =
    | "topic_spike"
    | "sentiment_drop"
    | "churn_surge"
    | "methodology_drop";

export type Severity = "high" | "medium" | "low";

export interface ManagerAlert {
    id: string;
    kind: AlertKind;
    severity: Severity;
    title: string;
    body: string | null;
    evidence: Record<string, unknown>;
    opened_at: string;
    acknowledged_at: string | null;
    dismissed_at: string | null;
    resolved_at: string | null;
}

export type RecommendationCategory =
    | "coach_rep"
    | "run_campaign"
    | "outreach_at_risk_customer"
    | "promote_winning_script";

export interface ManagerRecommendation {
    id: string;
    category: RecommendationCategory;
    title: string;
    rationale: string | null;
    evidence: Record<string, unknown>;
    target: Record<string, unknown>;
    score: number;
    status: "open" | "applied" | "dismissed" | "expired";
    applied_artifact_type: string | null;
    applied_artifact_id: string | null;
    expires_at: string;
    created_at: string;
}

export interface AlertConfig {
    inapp_enabled: boolean;
    slack_enabled: boolean;
    slack_min_severity: Severity;
    topic_spike_pct_change_threshold: number | null;
    topic_spike_min_volume: number | null;
    sentiment_drop_threshold: number | null;
    churn_surge_multiplier: number | null;
    methodology_drop_threshold: number | null;
}

export interface TrainingGapRow {
    rep_id: string | null;
    rep_name: string | null;
    call_count: number;
    reflection_rate: number | null;
    open_question_rate: number | null;
    avg_methodology_coverage: number | null;
}

export interface TrainingGapReport {
    window_days: number;
    rows: TrainingGapRow[];
}

// ── Hooks ─────────────────────────────────────────────────────────────

export function useManagerNarrative() {
    const api = useApi();
    return useQuery({
        queryKey: ["manager", "narrative"],
        queryFn: () => api.get<ManagerNarrative>("/manager/narrative"),
        refetchOnWindowFocus: false,
    });
}

export function useRefreshNarrative() {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: () => api.post<{ enqueued: boolean }>("/manager/narrative/refresh"),
        onSuccess: () => {
            qc.invalidateQueries({ queryKey: ["manager", "narrative"] });
        },
    });
}

export function useManagerAlerts(opts: { onlyOpen?: boolean } = {}) {
    const api = useApi();
    const onlyOpen = opts.onlyOpen ?? true;
    return useQuery({
        queryKey: ["manager", "alerts", { onlyOpen }],
        queryFn: () =>
            api.get<ManagerAlert[]>(`/manager/alerts?only_open=${onlyOpen}`),
        refetchInterval: 60_000,
    });
}

export function useAcknowledgeAlert() {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: (alertId: string) =>
            api.post(`/manager/alerts/${alertId}/acknowledge`),
        onSuccess: () => {
            qc.invalidateQueries({ queryKey: ["manager", "alerts"] });
        },
    });
}

export function useDismissAlert() {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: ({ id, reason }: { id: string; reason?: string }) =>
            api.post(`/manager/alerts/${id}/dismiss`, { reason }),
        onSuccess: () => {
            qc.invalidateQueries({ queryKey: ["manager", "alerts"] });
        },
    });
}

export function useManagerRecommendations(status: string = "open") {
    const api = useApi();
    return useQuery({
        queryKey: ["manager", "recommendations", status],
        queryFn: () =>
            api.get<ManagerRecommendation[]>(
                `/manager/recommendations?status=${status}`,
            ),
        refetchOnWindowFocus: false,
    });
}

export interface ApplyResult {
    artifact_type: string;
    artifact_id: string;
}

export function useApplyRecommendation() {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: (id: string) =>
            api.post<ApplyResult>(`/manager/recommendations/${id}/apply`),
        onSuccess: () => {
            qc.invalidateQueries({ queryKey: ["manager", "recommendations"] });
        },
    });
}

export function useDismissRecommendation() {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: ({ id, reason }: { id: string; reason?: string }) =>
            api.post(`/manager/recommendations/${id}/dismiss`, { reason }),
        onSuccess: () => {
            qc.invalidateQueries({ queryKey: ["manager", "recommendations"] });
        },
    });
}

export function useAlertConfig() {
    const api = useApi();
    return useQuery({
        queryKey: ["manager", "alert-config"],
        queryFn: () => api.get<AlertConfig>("/manager/alert-config"),
    });
}

export function useUpdateAlertConfig() {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: (patch: Partial<AlertConfig>) =>
            api.put<AlertConfig>("/manager/alert-config", patch),
        onSuccess: () => {
            qc.invalidateQueries({ queryKey: ["manager", "alert-config"] });
        },
    });
}

export function useTrainingGap(windowDays: number) {
    const api = useApi();
    return useQuery({
        queryKey: ["manager", "training-gap", windowDays],
        queryFn: () =>
            api.get<TrainingGapReport>(
                `/manager/training-gap?window_days=${windowDays}`,
            ),
    });
}
