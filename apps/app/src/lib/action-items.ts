"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useApi } from "./api";

export type ActionItemStatus =
    | "pending"
    | "open"
    | "done"
    | "completed"
    | "snoozed"
    | "dismissed"
    | "rejected"
    | string;

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
    automation_status: string;
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
}

export interface ActionItemPatch {
    status?: string;
    assigned_to?: string | null;
    priority?: string;
    due_date?: string | null;
    title?: string;
    description?: string | null;
    automation_status?: string;
}

function buildQueryString(filters: ActionItemFilters): string {
    const params = new URLSearchParams();
    if (filters.status) params.set("status", filters.status);
    if (filters.assigned_to) params.set("assigned_to", filters.assigned_to);
    if (filters.priority) params.set("priority", filters.priority);
    if (filters.category) params.set("category", filters.category);
    if (filters.limit !== undefined) params.set("limit", String(filters.limit));
    if (filters.offset !== undefined) params.set("offset", String(filters.offset));
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
