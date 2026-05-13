"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useApi } from "./api";

// Phase 5B simplified the canon to {open, done, dismissed}; legacy
// spellings normalize at the API layer but we keep the type wide so
// older fixtures don't trip TypeScript.
export type ActionItemStatus =
    | "open"
    | "done"
    | "dismissed"
    | "pending"
    | "completed"
    | "snoozed"
    | "rejected"
    | string;

export interface ActionItemParticipant {
    name: string;
    email?: string | null;
    role?: string | null;
    side?: "customer" | "vendor" | string | null;
    source?: string | null;
}

export interface SuggestedAttachment {
    title: string;
    reason?: string | null;
    kb_doc_id?: string | null;
}

export interface SentAttachment {
    kind: "kb" | "upload" | string;
    id: string;
    title?: string | null;
    filename?: string | null;
    mime_type?: string | null;
    sent_at?: string | null;
}

export interface ActionItem {
    id: string;
    interaction_id: string;
    tenant_id: string;
    assigned_to: string | null;
    title: string;
    description: string | null;
    category: string | null;
    priority: string;
    status: ActionItemStatus;
    due_date: string | null;
    calendar_event_id: string | null;
    email_draft: Record<string, unknown> | null;
    call_script: string[] | null;
    next_step_type: string | null;
    recommended_channel: string | null;
    channel_reasoning: string | null;
    participants: ActionItemParticipant[];
    prep_artifacts: string[];
    parent_action_item_id: string | null;
    implicit_signal: string | null;
    suggested_attachments: SuggestedAttachment[];
    attachments_sent: SentAttachment[];
    manually_created: boolean;
    feedback_score: number;
    automation_status: string;
    dismiss_reason: string | null;
    snoozed_until: string | null;
    completed_at: string | null;
    dismissed_at: string | null;
    created_at: string;
}

export interface ActionItemFilters {
    status?: string;
    assigned_to?: string;
    priority?: string;
    category?: string;
    q?: string;
    limit?: number;
    offset?: number;
    // Server-side filter: only items past due_date AND still open.
    overdue?: boolean;
}

export interface ActionItemPatch {
    status?: string;
    assigned_to?: string | null;
    priority?: string;
    due_date?: string | null;
    title?: string;
    description?: string | null;
    automation_status?: string;
    dismiss_reason?: string | null;
    snoozed_until?: string | null;
    call_script?: string[] | null;
    email_draft?: Record<string, unknown> | null;
    next_step_type?: string | null;
    recommended_channel?: string | null;
    channel_reasoning?: string | null;
    participants?: ActionItemParticipant[] | null;
    prep_artifacts?: string[] | null;
    user_id?: string | null;
}

function buildQueryString(filters: ActionItemFilters): string {
    const params = new URLSearchParams();
    if (filters.status) params.set("status", filters.status);
    if (filters.assigned_to) params.set("assigned_to", filters.assigned_to);
    if (filters.priority) params.set("priority", filters.priority);
    if (filters.category) params.set("category", filters.category);
    if (filters.limit !== undefined) params.set("limit", String(filters.limit));
    if (filters.offset !== undefined) params.set("offset", String(filters.offset));
    if (filters.overdue) params.set("overdue", "true");
    const qs = params.toString();
    return qs ? `?${qs}` : "";
}

export function useActionItems(filters: ActionItemFilters = {}) {
    const api = useApi();
    return useQuery({
        queryKey: ["action-items", filters],
        queryFn: async () => {
            const items = await api.get<ActionItem[]>(
                `/action-items${buildQueryString(filters)}`,
            );
            // Server doesn't filter by `q` — match locally so the UI
            // search box still works without a round-trip schema change.
            if (filters.q) {
                const needle = filters.q.toLowerCase();
                return items.filter(
                    (it) =>
                        it.title?.toLowerCase().includes(needle) ||
                        it.description?.toLowerCase().includes(needle),
                );
            }
            return items;
        },
    });
}

export function useActionItem(id: string | undefined) {
    const api = useApi();
    return useQuery({
        queryKey: ["action-item", id],
        queryFn: () => api.get<ActionItem>(`/action-items/${id}`),
        enabled: Boolean(id),
    });
}

export function useUpdateActionItem() {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: ({ id, patch }: { id: string; patch: ActionItemPatch }) =>
            api.patch<ActionItem>(`/action-items/${id}`, patch),
        onMutate: async ({ id, patch }) => {
            await qc.cancelQueries({ queryKey: ["action-items"] });
            const snapshot = qc.getQueriesData<ActionItem[]>({
                queryKey: ["action-items"],
            });
            for (const [key, data] of snapshot) {
                if (!data) continue;
                qc.setQueryData<ActionItem[]>(
                    key,
                    data.map((it) =>
                        it.id === id ? { ...it, ...patch } as ActionItem : it,
                    ),
                );
            }
            return { snapshot };
        },
        onError: (_err, _vars, ctx) => {
            if (!ctx) return;
            for (const [key, data] of ctx.snapshot) {
                qc.setQueryData(key, data);
            }
        },
        onSettled: () => {
            qc.invalidateQueries({ queryKey: ["action-items"] });
            qc.invalidateQueries({ queryKey: ["action-item"] });
        },
    });
}

// ── Comments ────────────────────────────────────────────────────────────

export interface ActionItemComment {
    id: string;
    action_item_id: string | null;
    interaction_id: string | null;
    user_id: string;
    body: string;
    created_at: string;
}

export function useActionItemComments(actionItemId: string | undefined) {
    const api = useApi();
    return useQuery({
        queryKey: ["action-item-comments", actionItemId],
        queryFn: () =>
            api.get<ActionItemComment[]>(
                `/action-items/${actionItemId}/comments`,
            ),
        enabled: Boolean(actionItemId),
    });
}

export function useAddActionItemComment() {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: ({ id, body }: { id: string; body: string }) =>
            api.post<ActionItemComment>(`/action-items/${id}/comments`, {
                body,
            }),
        onSuccess: (_data, vars) => {
            qc.invalidateQueries({
                queryKey: ["action-item-comments", vars.id],
            });
        },
    });
}

// ── Reject + return ─────────────────────────────────────────────────────

export function useReturnActionItem() {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: ({ id, reason }: { id: string; reason: string }) =>
            api.post<ActionItem>(`/action-items/${id}/return`, { reason }),
        onSuccess: () => {
            qc.invalidateQueries({ queryKey: ["action-items"] });
            qc.invalidateQueries({ queryKey: ["action-item"] });
        },
    });
}

// ── Schedule meeting ────────────────────────────────────────────────────

export interface ScheduleMeetingPayload {
    start?: string | null;
    duration_minutes?: number;
    location?: string | null;
    override_subject?: string | null;
    override_participants?: ActionItemParticipant[] | null;
    conference_provider?: string | null;
}

export interface ScheduleMeetingResult {
    success: boolean;
    provider: string;
    event_id: string | null;
    join_url: string | null;
    html_link: string | null;
    ics_payload: string | null;
    note: string | null;
    error: string | null;
}

export function useScheduleMeeting() {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: ({
            id,
            payload,
        }: {
            id: string;
            payload: ScheduleMeetingPayload;
        }) =>
            api.post<ScheduleMeetingResult>(
                `/action-items/${id}/schedule-meeting`,
                payload,
            ),
        onSuccess: () => {
            qc.invalidateQueries({ queryKey: ["action-items"] });
            qc.invalidateQueries({ queryKey: ["action-item"] });
        },
    });
}

// ── Feedback (helpful / not helpful) ────────────────────────────────────

export function useActionItemFeedback() {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: ({
            id,
            helpful,
            note,
        }: {
            id: string;
            helpful: boolean;
            note?: string;
        }) =>
            api.post<ActionItem>(`/action-items/${id}/feedback`, {
                helpful,
                note,
            }),
        onSuccess: () => {
            qc.invalidateQueries({ queryKey: ["action-items"] });
            qc.invalidateQueries({ queryKey: ["action-item"] });
        },
    });
}

// ── Manual create ───────────────────────────────────────────────────────

export interface ActionItemCreatePayload {
    interaction_id: string;
    title: string;
    description?: string | null;
    category?: string | null;
    priority?: string;
    due_date?: string | null;
    assigned_to?: string | null;
    next_step_type?: string | null;
    recommended_channel?: string | null;
    participants?: ActionItemParticipant[];
    prep_artifacts?: string[];
}

export function useCreateActionItem() {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: (payload: ActionItemCreatePayload) =>
            api.post<ActionItem>("/action-items", payload),
        onSuccess: () => {
            qc.invalidateQueries({ queryKey: ["action-items"] });
        },
    });
}

export interface TenantUser {
    id: string;
    name: string | null;
}

// Backed by /users/lookup (id + name only) — non-admin-callable so
// managers and agents can populate assignee pickers without 403ing
// against the admin /users endpoint.
export function useTenantUsers() {
    const api = useApi();
    return useQuery({
        queryKey: ["tenant-users-lookup"],
        queryFn: () => api.get<TenantUser[]>("/users/lookup"),
        retry: false,
        staleTime: 60_000,
    });
}
