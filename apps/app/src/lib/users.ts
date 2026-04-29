"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useApi } from "./api";
import type { UserRole } from "./me";

export interface TeamUser {
    id: string;
    tenant_id: string;
    email: string;
    name: string | null;
    role: UserRole;
    is_active: boolean;
    last_login_at: string | null;
    created_at: string;
}

export interface UserCreatePayload {
    email: string;
    name?: string;
    role: UserRole;
    password: string;
}

export interface UserPatchPayload {
    name?: string;
    role?: UserRole;
    is_active?: boolean;
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
        // Non-admins 403 here; SPA hides the banner in that case.
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
