"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import { useApi } from "./api";

export type BackfillStatus = "queued" | "running" | "done" | "error";

export interface BackfillStartResponse {
    job_id: string;
    status: BackfillStatus;
    window_days: number;
}

export interface BackfillJob {
    job_id: string;
    provider: string;
    status: BackfillStatus;
    window_days: number;
    fetched: number;
    ingested: number;
    skipped: number;
    error: string | null;
    started_at: string | null;
    finished_at: string | null;
}

// Kick off a 90-day historical import for a connected mailbox. Returns
// the job id the caller polls with useBackfillJob. If a sync is already
// queued/running for this mailbox, the API returns that job's handle
// instead of starting a new one, so polling resumes on the in-flight job.
export function useStartBackfill() {
    const api = useApi();
    return useMutation({
        mutationFn: (vars: { provider: string; days?: number }) =>
            api.post<BackfillStartResponse>("/email/backfill", {
                provider: vars.provider,
                days: vars.days ?? 90,
            }),
    });
}

// Poll a backfill job. Polls every 2s while queued/running, then stops —
// once the job is done/error the counters won't change again.
export function useBackfillJob(jobId: string | null) {
    const api = useApi();
    return useQuery({
        queryKey: ["email-backfill", jobId],
        enabled: !!jobId,
        queryFn: () => api.get<BackfillJob>(`/email/backfill/${jobId}`),
        refetchInterval: (query) => {
            const s = query.state.data?.status;
            return s === "queued" || s === "running" ? 2000 : false;
        },
    });
}
