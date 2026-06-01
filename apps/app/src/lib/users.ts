"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useApi } from "./api";
import type { Domain, UserRole } from "./me";

export interface TeamUser {
    id: string;
    tenant_id: string;
    email: string;
    name: string | null;
    role: UserRole;
    is_active: boolean;
    last_login_at: string | null;
    created_at: string;
    // ── Motion scopes (added with admin UI build) ──────────────────────
    agent_domains: Domain[];
    manager_domains: Domain[];
    is_tenant_admin: boolean;
}

export interface UserCreatePayload {
    email: string;
    name?: string;
    role: UserRole;
    password: string;
    agent_domains?: Domain[];
    manager_domains?: Domain[];
    is_tenant_admin?: boolean;
}

export interface UserPatchPayload {
    name?: string;
    role?: UserRole;
    is_active?: boolean;
    agent_domains?: Domain[];
    manager_domains?: Domain[];
    is_tenant_admin?: boolean;
}

export function useUsers(includeInactive = false) {
    const api = useApi();
    return useQuery({
        queryKey: ["users", { includeInactive }],
        queryFn: () =>
            api.get<TeamUser[]>(
                `/users${includeInactive ? "?include_inactive=true" : ""}`,
            ),
    });
}

export function useCreateUser() {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: (payload: UserCreatePayload) =>
            api.post<TeamUser>("/users", payload),
        onSuccess: () => {
            qc.invalidateQueries({ queryKey: ["users"] });
        },
    });
}

export function usePatchUser() {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: ({ id, patch }: { id: string; patch: UserPatchPayload }) =>
            api.patch<TeamUser>(`/users/${id}`, patch),
        onSuccess: () => {
            qc.invalidateQueries({ queryKey: ["users"] });
            qc.invalidateQueries({ queryKey: ["me"] });
        },
    });
}

export function useDeactivateUser() {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: (id: string) => api.del<void>(`/users/${id}`),
        onSuccess: () => {
            qc.invalidateQueries({ queryKey: ["users"] });
        },
    });
}

export interface SeatReconciliation {
    pending: boolean;
    seat_limit: number;
    admin_seat_limit: number;
    active_users: number;
    active_admins: number;
    suspended_users: TeamUser[];
}

export function useSeatReconciliation() {
    const api = useApi();
    return useQuery({
        queryKey: ["seat-reconciliation"],
        queryFn: () =>
            api.get<SeatReconciliation>("/admin/seat-reconciliation"),
        retry: false,
        staleTime: 30_000,
    });
}

export function useReactivateUser() {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: ({
            id,
            suspendSwapUserId,
        }: {
            id: string;
            suspendSwapUserId?: string;
        }) =>
            api.post<TeamUser>(`/users/${id}/reactivate`, {
                suspend_swap_user_id: suspendSwapUserId ?? null,
            }),
        onSuccess: () => {
            qc.invalidateQueries({ queryKey: ["users"] });
            qc.invalidateQueries({ queryKey: ["seat-reconciliation"] });
        },
    });
}

// ── Tenant default motion ─────────────────────────────────────────────

export interface TenantDefaultMotion {
    default_domain: Domain;
}

export function useTenantDefaultMotion() {
    const api = useApi();
    return useQuery({
        queryKey: ["admin", "tenant-default-motion"],
        queryFn: () =>
            api.get<TenantDefaultMotion>("/admin/tenant/default-motion"),
        retry: false,
    });
}

export function useSetTenantDefaultMotion() {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: (payload: { default_domain: Domain }) =>
            api.put<TenantDefaultMotion>(
                "/admin/tenant/default-motion",
                payload,
            ),
        onSuccess: () => {
            qc.invalidateQueries({ queryKey: ["admin", "tenant-default-motion"] });
        },
    });
}

// ── CSV import ────────────────────────────────────────────────────────

export interface UserImportRowResult {
    line_number: number;
    email: string | null;
    user_id: string | null;
    error: string | null;
}

export interface UserImportSummary {
    total_rows: number;
    created: number;
    skipped: number;
    rows: UserImportRowResult[];
}

export function useImportUsers() {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: async (file: File): Promise<UserImportSummary> => {
            const form = new FormData();
            form.append("file", file);
            // ``api.post`` JSONifies its body; for multipart we drop down
            // to fetch directly. ``api.token()`` exposes the bearer the
            // useApi hook is using, so we don't reach into local storage.
            const resp = await api.fetchRaw("/admin/users/import", {
                method: "POST",
                body: form,
            });
            if (!resp.ok) {
                const text = await resp.text();
                throw new Error(text || `Import failed (${resp.status})`);
            }
            return (await resp.json()) as UserImportSummary;
        },
        onSuccess: () => {
            qc.invalidateQueries({ queryKey: ["users"] });
            qc.invalidateQueries({ queryKey: ["seat-reconciliation"] });
        },
    });
}
