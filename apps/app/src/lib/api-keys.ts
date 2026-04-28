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
}

export interface ApiKeyCreated extends ApiKey {
    key: string;
}

export interface ApiKeyCreatePayload {
    name?: string;
    scopes?: string[];
    expires_at?: string;
}

export function useApiKeys() {
    const api = useApi();
    return useQuery({
        queryKey: ["api-keys"],
        queryFn: () => api.get<ApiKey[]>("/api-keys"),
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
