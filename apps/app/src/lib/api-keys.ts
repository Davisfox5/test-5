"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useApi } from "./api";

export interface ApiKey {
    id: string;
    name: string | null;
    scopes: string[];
    last_used_at: string | null;
    expires_at: string | null;
    created_at: string;
    revoked_at?: string | null;
}

export interface ApiKeyCreated extends ApiKey {
    key: string;
}

export interface ApiKeyCreatePayload {
    name?: string;
    expires_at?: string;
    /** When omitted the backend assigns its read-only default set. */
    scopes?: string[];
}

export interface ApiKeyUpdatePayload {
    name?: string | null;
    expires_at?: string | null;
    scopes?: string[];
}

export interface ScopeCatalog {
    scopes: string[];
    default_read_only: string[];
}

export function useApiKeys() {
    const api = useApi();
    return useQuery({
        queryKey: ["api-keys"],
        queryFn: () => api.get<ApiKey[]>("/api-keys"),
    });
}

export function useScopeCatalog() {
    const api = useApi();
    return useQuery({
        queryKey: ["api-keys", "scopes"],
        queryFn: () => api.get<ScopeCatalog>("/api-keys/scopes"),
        staleTime: 5 * 60 * 1000, // canonical list — barely changes
    });
}

export function useCreateApiKey() {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: (payload: ApiKeyCreatePayload) =>
            api.post<ApiKeyCreated>("/api-keys", payload),
        onSuccess: () => {
            qc.invalidateQueries({ queryKey: ["api-keys"] });
        },
    });
}

export function useUpdateApiKey() {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: ({ id, payload }: { id: string; payload: ApiKeyUpdatePayload }) =>
            api.patch<ApiKey>(`/api-keys/${id}`, payload),
        onSuccess: () => {
            qc.invalidateQueries({ queryKey: ["api-keys"] });
        },
    });
}

export function useRevokeApiKey() {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: (id: string) => api.del<void>(`/api-keys/${id}`),
        onSuccess: () => {
            qc.invalidateQueries({ queryKey: ["api-keys"] });
        },
    });
}
