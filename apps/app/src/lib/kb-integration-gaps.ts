"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { useApi } from "./api";

/**
 * Admin "KB-integration alignment" report.
 *
 * Each row is a procedure (KB chunk) that references an integration
 * the tenant hasn't connected. Drives the screen that lets admins
 * either connect the missing integration or revise the procedure.
 */

export interface KbIntegrationGap {
    id: string;
    chunk_id: string;
    doc_id: string;
    doc_title: string | null;
    procedure_title: string | null;
    required_provider: string;
    operation: string | null;
    compliance_level: "must" | "should" | "may";
    detected_at: string;
}

export interface KbIntegrationGapsResponse {
    items: KbIntegrationGap[];
    total: number;
    by_provider: Record<string, number>;
}

export function useKbIntegrationGaps(opts?: { provider?: string }) {
    const api = useApi();
    const path = opts?.provider
        ? `/admin/kb-integration-gaps?provider=${encodeURIComponent(opts.provider)}`
        : "/admin/kb-integration-gaps";
    return useQuery({
        queryKey: ["kb-integration-gaps", opts?.provider ?? "all"],
        queryFn: () => api.get<KbIntegrationGapsResponse>(path),
    });
}

export function useReevaluateKbIntegrationGaps() {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: () =>
            api.post<{ cleared: number; added: number }>(
                "/admin/kb-integration-gaps/reevaluate",
            ),
        onSuccess: () => {
            qc.invalidateQueries({ queryKey: ["kb-integration-gaps"] });
        },
    });
}
