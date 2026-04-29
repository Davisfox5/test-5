"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useApi } from "./api";

export interface Webhook {
    id: string;
    tenant_id: string;
    url: string;
    events: string[];
    active: boolean;
    created_at: string;
}

export interface WebhookCreated extends Webhook {
    secret: string;
}

export interface WebhookCreatePayload {
    url: string;
    events: string[];
    active?: boolean;
}

export interface WebhookPatchPayload {
    url?: string;
    events?: string[];
    active?: boolean;
}

export interface WebhookEvent {
    name: string;
    description: string;
}

export interface WebhookTestResult {
    status: string;
    status_code?: number | null;
    error?: string | null;
}

export function useWebhooks() {
    const api = useApi();
    return useQuery({
        queryKey: ["webhooks"],
        queryFn: () => api.get<Webhook[]>("/webhooks"),
    });
}

export function useWebhookEvents() {
    const api = useApi();
    return useQuery({
        queryKey: ["webhook-events"],
        queryFn: () =>
            api.get<{ events: WebhookEvent[] }>("/webhooks/events"),
        // Event catalog is static within a deploy; keep for the session.
        staleTime: 5 * 60_000,
    });
}

export function useCreateWebhook() {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: (payload: WebhookCreatePayload) =>
            api.post<WebhookCreated>("/webhooks", payload),
        onSuccess: () => qc.invalidateQueries({ queryKey: ["webhooks"] }),
    });
}

export function usePatchWebhook() {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: ({
            id,
            patch,
        }: {
            id: string;
            patch: WebhookPatchPayload;
        }) => api.patch<Webhook>(`/webhooks/${id}`, patch),
        onSuccess: () => qc.invalidateQueries({ queryKey: ["webhooks"] }),
    });
}

export function useDeleteWebhook() {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: (id: string) => api.del<void>(`/webhooks/${id}`),
        onSuccess: () => qc.invalidateQueries({ queryKey: ["webhooks"] }),
    });
}

export function useTestWebhook() {
    const api = useApi();
    return useMutation({
        mutationFn: (id: string) =>
            api.post<WebhookTestResult>(`/webhooks/${id}/test`),
    });
}

export interface WebhookDelivery {
    id: string;
    webhook_id: string;
    event: string;
    status: string;
    attempt_count: number;
    last_status_code: number | null;
    last_error: string | null;
    next_retry_at: string | null;
    delivered_at: string | null;
    created_at: string;
}

export function useWebhookDeliveries(id: string | null, enabled = true) {
    const api = useApi();
    return useQuery({
        queryKey: ["webhook-deliveries", id],
        queryFn: () =>
            api.get<WebhookDelivery[]>(`/webhooks/${id}/deliveries`),
        enabled: !!id && enabled,
        staleTime: 10_000,
    });
}
