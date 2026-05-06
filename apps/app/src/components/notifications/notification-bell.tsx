"use client";

import { useEffect, useRef, useState } from "react";
import {
    useNotifications,
    useMarkNotificationRead,
    useMarkAllNotificationsRead,
    type Notification,
} from "@/lib/notifications";

/**
 * Notification bell — small button + dropdown list.
 *
 * Pulled from the dedicated /notifications endpoints. Auto-refreshes
 * every 30s via React Query so the badge stays current without a
 * websocket.
 */
export function NotificationBell() {
    const [open, setOpen] = useState(false);
    const ref = useRef<HTMLDivElement>(null);
    const { data } = useNotifications(false);
    const markRead = useMarkNotificationRead();
    const markAll = useMarkAllNotificationsRead();

    useEffect(() => {
        if (!open) return;
        function onClickAway(e: MouseEvent) {
            if (ref.current && !ref.current.contains(e.target as Node)) {
                setOpen(false);
            }
        }
        window.addEventListener("mousedown", onClickAway);
        return () => window.removeEventListener("mousedown", onClickAway);
    }, [open]);

    const unread = data?.unread_count ?? 0;
    const items = data?.items ?? [];

    return (
        <div ref={ref} className="relative">
            <button
                type="button"
                onClick={() => setOpen((v) => !v)}
                aria-label={`Notifications${unread ? ` (${unread} unread)` : ""}`}
                className="relative rounded-md p-2 text-text-muted hover:bg-card-hover hover:text-text focus:outline-none focus:ring-2 focus:ring-primary"
            >
                <span aria-hidden>🔔</span>
                {unread > 0 && (
                    <span className="absolute -right-0.5 -top-0.5 flex h-4 min-w-4 items-center justify-center rounded-full bg-accent-rose px-1 text-[10px] font-bold text-white">
                        {unread > 99 ? "99+" : unread}
                    </span>
                )}
            </button>
            {open && (
                <div
                    role="dialog"
                    aria-label="Notifications"
                    className="absolute right-0 z-30 mt-1 w-96 rounded-md border border-border bg-card shadow-lg"
                >
                    <header className="flex items-center justify-between border-b border-border px-3 py-2">
                        <span className="text-sm font-semibold text-text">
                            Notifications
                        </span>
                        {unread > 0 && (
                            <button
                                type="button"
                                onClick={() => markAll.mutate()}
                                className="text-xs text-primary hover:underline"
                            >
                                Mark all read
                            </button>
                        )}
                    </header>
                    <ul className="max-h-96 overflow-y-auto">
                        {items.length === 0 ? (
                            <li className="px-3 py-6 text-center text-sm text-text-muted">
                                You're caught up.
                            </li>
                        ) : (
                            items.map((n) => (
                                <NotificationRow
                                    key={n.id}
                                    n={n}
                                    onClick={() => {
                                        if (!n.is_read) markRead.mutate(n.id);
                                        if (n.link_url) {
                                            window.location.href = n.link_url;
                                        }
                                    }}
                                />
                            ))
                        )}
                    </ul>
                </div>
            )}
        </div>
    );
}

function NotificationRow({
    n,
    onClick,
}: {
    n: Notification;
    onClick: () => void;
}) {
    return (
        <li
            className={`cursor-pointer border-b border-border-light px-3 py-2 last:border-b-0 hover:bg-card-hover ${
                n.is_read ? "" : "bg-primary-soft/40"
            }`}
            onClick={onClick}
        >
            <div className="flex items-start justify-between gap-2">
                <div className="flex-1">
                    <div className="text-sm font-medium text-text">{n.title}</div>
                    {n.body && (
                        <div className="mt-0.5 line-clamp-2 text-xs text-text-muted">
                            {n.body}
                        </div>
                    )}
                </div>
                <span className="whitespace-nowrap text-xs text-text-subtle">
                    {timeAgo(n.created_at)}
                </span>
            </div>
        </li>
    );
}

function timeAgo(iso: string): string {
    const ms = Date.now() - new Date(iso).getTime();
    const min = Math.floor(ms / 60_000);
    if (min < 1) return "now";
    if (min < 60) return `${min}m`;
    const hr = Math.floor(min / 60);
    if (hr < 24) return `${hr}h`;
    const day = Math.floor(hr / 24);
    return `${day}d`;
}
