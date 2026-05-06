"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useApi } from "./api";

export type NotificationKind =
    | "action_item_assigned"
    | "action_item_comment"
    | "action_item_returned"
    | "action_item_due_soon"
    | "action_item_overdue"
    | "manager_review_completed"
    | "scorecard_review_assigned"
    | "system"
    | "other"
    | string;

export interface Notification {
    id: string;
    tenant_id: string;
    user_id: string;
    kind: NotificationKind;
    title: string;
    body: string | null;
    link_url: string | null;
    action_item_id: string | null;
    interaction_id: string | null;
    is_read: boolean;
    read_at: string | null;
    created_at: string;
}

export interface NotificationList {
    items: Notification[];
    unread_count: number;
}

export function useNotifications(only_unread = false) {
    const api = useApi();
    return useQuery({
        queryKey: ["notifications", { only_unread }],
        queryFn: () =>
            api.get<NotificationList>(
                `/notifications${only_unread ? "?only_unread=true" : ""}`,
            ),
        // Light auto-refetch so the bell badge stays current without
        // requiring a websocket. 30s is the same staleTime the rest of
        // the app uses; refetchInterval makes it active.
        refetchInterval: 30_000,
        staleTime: 15_000,
    });
}

export function useUnreadCount() {
    const api = useApi();
    return useQuery({
        queryKey: ["notifications-unread"],
        queryFn: () =>
            api.get<{ unread_count: number }>("/notifications/unread-count"),
        refetchInterval: 30_000,
    });
}

export function useMarkNotificationRead() {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: (id: string) =>
            api.post<{ ok: boolean }>(`/notifications/${id}/read`, {}),
        onSuccess: () => {
            qc.invalidateQueries({ queryKey: ["notifications"] });
            qc.invalidateQueries({ queryKey: ["notifications-unread"] });
        },
    });
}

export function useMarkAllNotificationsRead() {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: () =>
            api.post<{ updated: number }>("/notifications/mark-all-read", {}),
        onSuccess: () => {
            qc.invalidateQueries({ queryKey: ["notifications"] });
            qc.invalidateQueries({ queryKey: ["notifications-unread"] });
        },
    });
}
