"use client";

import { useEffect } from "react";
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
    | "manager_alert"
    // Cross-motion (added with PR cross-motion-notifications)
    | "case_assigned"
    | "case_escalated"
    | "renewal_at_risk"
    | "qbr_overdue"
    | "system"
    | "other"
    | string;

// Human-readable labels for the tray. Kept here so tray components
// stay declarative; falls back to the raw kind string when missing.
export const KIND_LABEL: Record<string, string> = {
    action_item_assigned: "Action item",
    action_item_comment: "Comment",
    action_item_returned: "Returned",
    action_item_due_soon: "Due soon",
    action_item_overdue: "Overdue",
    manager_review_completed: "Review done",
    scorecard_review_assigned: "Review assigned",
    manager_alert: "Manager alert",
    case_assigned: "Case assigned",
    case_escalated: "Case escalated",
    renewal_at_risk: "Renewal at risk",
    qbr_overdue: "QBR overdue",
    system: "System",
    other: "Notification",
};

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

// Feature-gate the SSE path so a misbehaving stream can be turned off
// without a code change. Default ON; set NEXT_PUBLIC_NOTIF_SSE_ENABLED=false
// to fall back to the original 30s polling.
const SSE_ENABLED =
    typeof process !== "undefined" &&
    process.env.NEXT_PUBLIC_NOTIF_SSE_ENABLED !== "false";

const SSE_SUPPORTED =
    typeof window !== "undefined" && typeof window.EventSource !== "undefined";

/**
 * Subscribe to push notifications via SSE. When connected, each event
 * invalidates the React Query caches so the bell badge + list refresh
 * without polling. Falls back silently when SSE is unsupported or the
 * stream errors — the existing query polling remains the safety net.
 */
function useNotificationsSSE() {
    const qc = useQueryClient();
    useEffect(() => {
        if (!SSE_ENABLED || !SSE_SUPPORTED) return;
        let es: EventSource | null = null;
        try {
            es = new EventSource("/api/notifications/stream", {
                withCredentials: true,
            });
        } catch {
            return; // graceful: caller still has the polling fallback
        }
        const onMessage = () => {
            qc.invalidateQueries({ queryKey: ["notifications"] });
            qc.invalidateQueries({ queryKey: ["notifications-unread"] });
        };
        es.addEventListener("notification", onMessage);
        es.addEventListener("error", () => {
            // Browser will auto-reconnect with exponential backoff. Just
            // log; the query polling fallback below keeps state fresh.
            // eslint-disable-next-line no-console
            console.debug("notifications SSE error; relying on polling");
        });
        return () => {
            es?.removeEventListener("notification", onMessage);
            es?.close();
        };
    }, [qc]);
}

export function useNotifications(only_unread = false) {
    const api = useApi();
    useNotificationsSSE();
    return useQuery({
        queryKey: ["notifications", { only_unread }],
        queryFn: () =>
            api.get<NotificationList>(
                `/notifications${only_unread ? "?only_unread=true" : ""}`,
            ),
        // Polling slows to 5 minutes when SSE is doing the live updates;
        // when SSE is disabled or unsupported this is the only refresh
        // signal, so it falls back to 30s.
        refetchInterval:
            SSE_ENABLED && SSE_SUPPORTED ? 300_000 : 30_000,
        staleTime: 15_000,
    });
}

export function useUnreadCount() {
    const api = useApi();
    useNotificationsSSE();
    return useQuery({
        queryKey: ["notifications-unread"],
        queryFn: () =>
            api.get<{ unread_count: number }>("/notifications/unread-count"),
        refetchInterval:
            SSE_ENABLED && SSE_SUPPORTED ? 300_000 : 30_000,
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
