"use client";

import { useQuery } from "@tanstack/react-query";
import { useApi } from "./api";

/** One row of the comprehensive audit log. */
export interface AuditLogRow {
    id: string;
    tenant_id: string;
    actor_user_id: string | null;
    actor_principal: "user" | "api_key" | "system";
    action: string;
    resource_type: string;
    resource_id: string | null;
    before: Record<string, unknown> | null;
    after: Record<string, unknown> | null;
    meta: Record<string, unknown>;
    created_at: string;
}

export interface AuditLogPage {
    items: AuditLogRow[];
    total: number;
    limit: number;
    offset: number;
}

export interface AuditLogFilters {
    action?: string;
    resource_type?: string;
    /** UUID of a user, or "user" / "api_key" / "system". */
    actor?: string;
    from?: string;
    to?: string;
    limit?: number;
    offset?: number;
}

export function useAuditLogs(filters: AuditLogFilters) {
    const api = useApi();
    const params = new URLSearchParams();
    if (filters.action) params.set("action", filters.action);
    if (filters.resource_type)
        params.set("resource_type", filters.resource_type);
    if (filters.actor) params.set("actor", filters.actor);
    if (filters.from) params.set("from", filters.from);
    if (filters.to) params.set("to", filters.to);
    params.set("limit", String(filters.limit ?? 50));
    params.set("offset", String(filters.offset ?? 0));

    return useQuery({
        queryKey: ["audit-logs", params.toString()],
        queryFn: () =>
            api.get<AuditLogPage>(`/admin/audit-logs?${params.toString()}`),
    });
}
