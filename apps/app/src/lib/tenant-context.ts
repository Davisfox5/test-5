"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useApi } from "./api";

export interface TenantContext {
    tenant_id: string;
    brief: Record<string, unknown>;
    prompt_preview?: string;
}

export interface TenantContextFieldsPatch {
    goals?: string[];
    kpis?: Array<Record<string, unknown>>;
    strategies?: string[];
    org_structure?: Record<string, unknown>;
    personal_touches?: Record<string, unknown>;
}

export function useTenantContext() {
    const api = useApi();
    return useQuery({
        queryKey: ["tenant-context"],
        queryFn: () => api.get<TenantContext>("/admin/tenant-context"),
    });
}

export function useUpdateTenantContextFields() {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: (patch: TenantContextFieldsPatch) =>
            api.request<{
                tenant_id: string;
                updated_keys: string[];
                brief: Record<string, unknown>;
            }>("/admin/tenant-context/fields", {
                method: "PUT",
                body: JSON.stringify(patch),
            }),
        onSuccess: () => qc.invalidateQueries({ queryKey: ["tenant-context"] }),
    });
}

export function useRebuildTenantContext() {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: () =>
            api.post<{ tenant_id: string; mode: string; scheduled?: boolean }>(
                "/admin/tenant-context/rebuild",
            ),
        onSuccess: () => qc.invalidateQueries({ queryKey: ["tenant-context"] }),
    });
}
