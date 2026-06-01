"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useApi } from "./api";

export type KBRequestStatus = "open" | "in_progress" | "published" | "dismissed";
export type KBRequestPriority = "high" | "medium" | "low";

export interface KBRequest {
    id: string;
    topic: string;
    rationale: string | null;
    proposed_body: string | null;
    status: KBRequestStatus;
    priority: KBRequestPriority;
    requested_by_user_id: string | null;
    assigned_to: string | null;
    assigned_to_name: string | null;
    source_recommendation_id: string | null;
    source_kb_chunk_id: string | null;
    created_at: string;
    published_at: string | null;
    dismissed_at: string | null;
}

export function useKBRequests(opts: { status?: KBRequestStatus; mineOnly?: boolean } = {}) {
    const api = useApi();
    return useQuery({
        queryKey: ["kb", "requests", opts],
        queryFn: () => {
            const qp = new URLSearchParams();
            if (opts.status) qp.set("status", opts.status);
            if (opts.mineOnly) qp.set("mine_only", "true");
            return api.get<KBRequest[]>(
                `/kb/requests${qp.toString() ? `?${qp}` : ""}`,
            );
        },
    });
}

export function useCreateKBRequest() {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: (body: {
            topic: string;
            rationale?: string;
            proposed_body?: string;
            priority?: KBRequestPriority;
            source_kb_chunk_id?: string;
        }) => api.post<KBRequest>("/kb/requests", body),
        onSuccess: () => {
            qc.invalidateQueries({ queryKey: ["kb", "requests"] });
        },
    });
}

export function usePatchKBRequest() {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: ({
            id,
            patch,
        }: {
            id: string;
            patch: Partial<
                Pick<
                    KBRequest,
                    "status" | "priority" | "assigned_to" | "proposed_body"
                >
            > & { dismiss_reason?: string };
        }) => api.patch<KBRequest>(`/kb/requests/${id}`, patch),
        onSuccess: () => {
            qc.invalidateQueries({ queryKey: ["kb", "requests"] });
        },
    });
}

export const STATUS_LABEL: Record<KBRequestStatus, string> = {
    open: "Open",
    in_progress: "In progress",
    published: "Published",
    dismissed: "Dismissed",
};

export const PRIORITY_LABEL: Record<KBRequestPriority, string> = {
    high: "High",
    medium: "Medium",
    low: "Low",
};
