"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useApi } from "./api";

/* ── Types ──────────────────────────────────────────────────────────── */

// Backend's CRM service supports these providers (sync_service.SUPPORTED_PROVIDERS).
export type CrmProvider = "pipedrive" | "hubspot" | "salesforce";

export interface CrmSyncSummary {
    provider: string;
    status: string;
    customers_upserted: number;
    contacts_upserted: number;
    briefs_rebuilt: number;
    error: string | null;
}

export interface CrmSyncLog {
    id: string;
    provider: string;
    status: string;
    customers_upserted: number;
    contacts_upserted: number;
    briefs_rebuilt: number;
    error: string | null;
    started_at: string;
    finished_at: string | null;
}

/* ── Queries ────────────────────────────────────────────────────────── */

export function useCrmSyncLogs(provider?: string, limit = 20) {
    const api = useApi();
    return useQuery({
        queryKey: ["crm-sync-logs", provider ?? null, limit],
        queryFn: () => {
            const sp = new URLSearchParams();
            sp.set("limit", String(limit));
            if (provider) sp.set("provider", provider);
            return api.get<CrmSyncLog[]>(`/crm/sync/logs?${sp.toString()}`);
        },
        // The list updates whenever a sync completes — refresh on focus
        // so admins return to the tab and immediately see the latest run.
        refetchOnWindowFocus: true,
    });
}

/* ── Mutations ──────────────────────────────────────────────────────── */

/**
 * Trigger a CRM sync for the given provider. The backend supports both
 * synchronous (returns the summary inline once done) and async modes;
 * we use the default sync=true so the SPA can show "X customers
 * upserted" once the response lands. For very large tenants the admin
 * can pass `sync=false` themselves via a future "background" toggle.
 */
export function useTriggerCrmSync() {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: (provider: CrmProvider) =>
            api.post<CrmSyncSummary>(`/crm/sync/${provider}`),
        onSettled: () => {
            qc.invalidateQueries({ queryKey: ["crm-sync-logs"] });
        },
    });
}
