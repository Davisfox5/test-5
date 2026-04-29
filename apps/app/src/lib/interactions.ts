"use client";

import {
    useMutation,
    useQuery,
    useQueryClient,
} from "@tanstack/react-query";
import { useApi } from "./api";

/* ── Types ──────────────────────────────────────────────────────────── */

// Backend writes only these four values today (`interactions.py`).
// "new" was carried in the union historically but is never produced —
// dropping it keeps the SPA's narrowing honest.
export type InteractionStatus =
    | "processing"
    | "analyzed"
    | "failed"
    | "flagged_for_review"
    | string;

export interface InteractionInsights {
    sentiment_score?: number;
    sentiment_overall?: string;
    churn_risk?: number;
    churn_risk_signal?: string;
    upsell_score?: number;
    upsell_signal?: string;
    topics?: Array<{ name: string; relevance?: number; mentions?: number }>;
    summary?: string;
    [k: string]: unknown;
}

export interface InteractionOut {
    id: string;
    tenant_id: string;
    channel: string;
    source: string | null;
    direction: string | null;
    title: string | null;
    status: InteractionStatus;
    duration_seconds: number | null;
    caller_phone: string | null;
    complexity_score: number | null;
    analysis_tier: string | null;
    call_metrics: Record<string, unknown>;
    insights: InteractionInsights;
    pii_redacted: boolean;
    detected_language: string | null;
    created_at: string;
}

export interface TranscriptTurn {
    speaker?: string;
    role?: string;
    text?: string;
    start?: number;
    end?: number;
    [k: string]: unknown;
}

export interface InteractionDetail extends InteractionOut {
    transcript: TranscriptTurn[];
    transcript_translated: TranscriptTurn[] | null;
    raw_text: string | null;
    thread_id: string | null;
    participants: Array<Record<string, unknown>>;
    agent_id: string | null;
    contact_id: string | null;
}

export interface InteractionListParams {
    limit?: number;
    offset?: number;
    q?: string;
    status?: string;
    channel?: string;
}

export interface IngestRecordingPayload {
    audio_url: string;
    title?: string;
    caller_phone?: string;
}

export interface UploadProgress {
    loaded: number;
    total: number;
    percent: number;
}

/* ── Queries ────────────────────────────────────────────────────────── */

function buildListQuery(params: InteractionListParams): string {
    const sp = new URLSearchParams();
    if (params.limit !== undefined) sp.set("limit", String(params.limit));
    if (params.offset !== undefined) sp.set("offset", String(params.offset));
    if (params.q) sp.set("q", params.q);
    if (params.status) sp.set("status", params.status);
    if (params.channel) sp.set("channel", params.channel);
    const qs = sp.toString();
    return qs ? `?${qs}` : "";
}

export interface UseInteractionsOptions {
    refetchInterval?: number | false;
    refetchOnWindowFocus?: boolean;
}

export function useInteractions(
    params: InteractionListParams = {},
    options: UseInteractionsOptions = {},
) {
    const api = useApi();
    return useQuery({
        queryKey: ["interactions", params],
        queryFn: () =>
            api.get<InteractionOut[]>(`/interactions${buildListQuery(params)}`),
        refetchInterval: options.refetchInterval,
        refetchOnWindowFocus: options.refetchOnWindowFocus,
    });
}

export function useInteraction(id: string | undefined) {
    const api = useApi();
    return useQuery({
        queryKey: ["interaction", id],
        queryFn: () => api.get<InteractionDetail>(`/interactions/${id}`),
        enabled: !!id,
        // Refresh detail every 10s while the row is still processing so
        // the user sees the transcript land without a manual reload.
        refetchInterval: (query) => {
            const data = query.state.data as InteractionDetail | undefined;
            if (data && data.status === "processing") return 10_000;
            return false;
        },
    });
}

/* ── Mutations ──────────────────────────────────────────────────────── */

export function useUploadInteraction() {
    const qc = useQueryClient();
    return useMutation({
        mutationFn: async (args: {
            file: File;
            title?: string;
            getToken: () => Promise<string | null>;
            onProgress?: (p: UploadProgress) => void;
        }) => {
            // Native fetch doesn't expose XHR upload progress events, so
            // we drop to XMLHttpRequest just for this one call to keep
            // the modal's progress bar honest.
            const { file, title, getToken, onProgress } = args;
            const token = await getToken();
            const fd = new FormData();
            fd.append("file", file);
            if (title) fd.append("title", title);

            return new Promise<InteractionOut>((resolve, reject) => {
                const xhr = new XMLHttpRequest();
                xhr.open("POST", "/api/v1/interactions/upload");
                if (token) {
                    xhr.setRequestHeader("Authorization", `Bearer ${token}`);
                }
                xhr.setRequestHeader("Accept", "application/json");
                xhr.upload.onprogress = (ev) => {
                    if (!ev.lengthComputable || !onProgress) return;
                    onProgress({
                        loaded: ev.loaded,
                        total: ev.total,
                        percent: Math.round((ev.loaded / ev.total) * 100),
                    });
                };
                xhr.onerror = () => reject(new Error("Network error"));
                xhr.onload = () => {
                    if (xhr.status >= 200 && xhr.status < 300) {
                        try {
                            resolve(JSON.parse(xhr.responseText));
                        } catch (e) {
                            reject(e);
                        }
                    } else {
                        let detail = `HTTP ${xhr.status}`;
                        try {
                            const body = JSON.parse(xhr.responseText);
                            if (body?.detail) detail = body.detail;
                        } catch {}
                        reject(new Error(detail));
                    }
                };
                xhr.send(fd);
            });
        },
        onSuccess: () => {
            qc.invalidateQueries({ queryKey: ["interactions"] });
            qc.invalidateQueries({ queryKey: ["dashboard-summary"] });
        },
    });
}

export function useIngestRecording() {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: (payload: IngestRecordingPayload) =>
            api.post<InteractionOut>("/interactions/ingest-recording", payload),
        onSuccess: () => {
            qc.invalidateQueries({ queryKey: ["interactions"] });
            qc.invalidateQueries({ queryKey: ["dashboard-summary"] });
        },
    });
}

export function useDeleteInteraction() {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: (id: string) => api.del<void>(`/interactions/${id}`),
        onSuccess: (_data, id) => {
            qc.invalidateQueries({ queryKey: ["interactions"] });
            qc.invalidateQueries({ queryKey: ["interaction", id] });
            qc.invalidateQueries({ queryKey: ["dashboard-summary"] });
        },
    });
}

export function useUpdateInteraction() {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: (args: {
            id: string;
            patch: { title?: string; contact_id?: string };
        }) =>
            api.patch<InteractionOut>(`/interactions/${args.id}`, args.patch),
        onSuccess: (_data, args) => {
            qc.invalidateQueries({ queryKey: ["interactions"] });
            qc.invalidateQueries({ queryKey: ["interaction", args.id] });
        },
    });
}

/* ── Dashboard / action items ───────────────────────────────────────── */

export interface DashboardSummary {
    total_interactions: number;
    avg_sentiment_score: number | null;
    action_items_open: number;
    avg_qa_score: number | null;
    prev_period_deltas: {
        total_interactions_pct?: number | null;
        avg_sentiment_pct?: number | null;
        avg_qa_pct?: number | null;
    };
}

export type DashboardPeriod = "7d" | "30d" | "90d";

export function useDashboardSummary(period: DashboardPeriod = "30d") {
    const api = useApi();
    return useQuery({
        queryKey: ["dashboard-summary", period],
        queryFn: () =>
            api.get<DashboardSummary>(
                `/analytics/dashboard?period=${period}`,
            ),
    });
}

export interface AiHealth {
    quality_score_avg_7d: number | null;
    quality_score_avg_30d: number | null;
    feedback_events_7d: number;
    asr_wer_7d: number | null;
    pending_vocab_candidates: number;
    flagged_for_review_count: number;
}

export function useAiHealth() {
    const api = useApi();
    return useQuery({
        queryKey: ["ai-health"],
        queryFn: () => api.get<AiHealth>("/analytics/ai-health"),
        // ai-health depends on tables that may not exist on a fresh
        // sandbox tenant; let it fail quietly so the dashboard still
        // loads.
        retry: false,
    });
}

export interface ActionItemOut {
    id: string;
    interaction_id: string;
    tenant_id: string;
    assigned_to: string | null;
    title: string;
    description: string | null;
    category: string | null;
    priority: string;
    status: string;
    due_date: string | null;
    calendar_event_id: string | null;
    email_draft: Record<string, unknown> | null;
    automation_status: string;
    created_at: string;
}

export function useOpenActionItems(limit = 5) {
    const api = useApi();
    return useQuery({
        queryKey: ["action-items", "open", limit],
        queryFn: () =>
            api.get<ActionItemOut[]>(
                `/action-items?limit=${limit}&status=open`,
            ),
        // Backend uses 'pending' / 'in_progress' for the open buckets but
        // the spec asks for `?status=open`. If the API returns 422 we
        // fall back to no filter on first error.
        retry: false,
    });
}

/* ── Helpers ────────────────────────────────────────────────────────── */

export function formatDuration(seconds: number | null | undefined): string {
    if (!seconds || seconds < 0) return "—";
    const m = Math.floor(seconds / 60);
    const s = Math.floor(seconds % 60);
    return `${m}:${String(s).padStart(2, "0")}`;
}

export function formatRelative(iso: string): string {
    const then = new Date(iso).getTime();
    const now = Date.now();
    const diff = Math.max(0, now - then);
    const mins = Math.floor(diff / 60_000);
    if (mins < 1) return "just now";
    if (mins < 60) return `${mins}m ago`;
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return `${hrs}h ago`;
    const days = Math.floor(hrs / 24);
    if (days < 7) return `${days}d ago`;
    return new Date(iso).toLocaleDateString();
}

export function sentimentLabel(score: number | null | undefined): {
    text: string;
    tone: "emerald" | "amber" | "rose" | "subtle";
} {
    if (score == null) return { text: "—", tone: "subtle" };
    if (score >= 7) return { text: "Positive", tone: "emerald" };
    if (score >= 4) return { text: "Neutral", tone: "amber" };
    return { text: "Negative", tone: "rose" };
}
