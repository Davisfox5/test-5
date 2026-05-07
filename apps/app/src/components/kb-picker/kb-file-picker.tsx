"use client";

import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useApi } from "@/lib/api";

/**
 * KB file picker — modal browser for tenant + agent KB docs.
 *
 * Used by the action-item / follow-up email composer to attach
 * supporting documents from the tenant-wide KB or the rep's personal
 * KB. Tabs gate the scope; the search box does an in-memory contains
 * match on title and tags; multi-select with confirm closes the
 * picker and hands the chosen docs back to the parent.
 */

interface KBDoc {
    id: string;
    tenant_id: string;
    owner_user_id: string | null;
    title: string | null;
    source_type: string | null;
    source_url: string | null;
    tags: string[];
    last_synced_at: string | null;
    created_at: string;
}

export interface KBPickedAttachment {
    kind: "kb";
    id: string;
    title: string;
}

type Scope = "all" | "tenant" | "personal";

export function KBFilePickerModal({
    open,
    onClose,
    onConfirm,
    initialSelection,
}: {
    open: boolean;
    onClose: () => void;
    onConfirm: (selected: KBPickedAttachment[]) => void;
    initialSelection?: KBPickedAttachment[];
}) {
    const [scope, setScope] = useState<Scope>("all");
    const [query, setQuery] = useState("");
    const [picked, setPicked] = useState<Map<string, KBPickedAttachment>>(
        () => new Map(initialSelection?.map((s) => [s.id, s])),
    );

    const api = useApi();
    const { data: docs = [], isLoading } = useQuery({
        queryKey: ["kb-docs", scope],
        queryFn: () => api.get<KBDoc[]>(`/kb/docs?scope=${scope}&limit=200`),
        enabled: open,
    });

    useEffect(() => {
        if (!open) return;
        function onKey(e: KeyboardEvent) {
            if (e.key === "Escape") onClose();
        }
        window.addEventListener("keydown", onKey);
        return () => window.removeEventListener("keydown", onKey);
    }, [open, onClose]);

    if (!open) return null;

    const needle = query.trim().toLowerCase();
    const filtered = needle
        ? docs.filter(
              (d) =>
                  (d.title || "").toLowerCase().includes(needle) ||
                  (d.tags || []).some((t) => t.toLowerCase().includes(needle)),
          )
        : docs;

    function toggle(d: KBDoc) {
        setPicked((prev) => {
            const next = new Map(prev);
            if (next.has(d.id)) {
                next.delete(d.id);
            } else {
                next.set(d.id, {
                    kind: "kb",
                    id: d.id,
                    title: d.title || "(untitled)",
                });
            }
            return next;
        });
    }

    return (
        <>
            <div
                className="fixed inset-0 z-40 bg-black/40 backdrop-blur-[2px]"
                onClick={onClose}
                aria-hidden
            />
            <div
                role="dialog"
                aria-label="Attach KB document"
                className="fixed left-1/2 top-1/2 z-50 flex h-[70vh] w-full max-w-2xl -translate-x-1/2 -translate-y-1/2 flex-col rounded-lg border border-border bg-bg-card shadow-xl"
            >
                <header className="flex items-center justify-between gap-2 border-b border-border px-4 py-3">
                    <h2 className="text-base font-semibold text-text">
                        Attach from Knowledge Base
                    </h2>
                    <button
                        type="button"
                        onClick={onClose}
                        aria-label="Close"
                        className="rounded-md p-1 text-text-muted hover:bg-card-hover hover:text-text focus:outline-none focus:ring-2 focus:ring-primary"
                    >
                        ✕
                    </button>
                </header>

                <div className="flex items-center gap-2 border-b border-border-light px-4 py-2">
                    {(["all", "tenant", "personal"] as Scope[]).map((s) => (
                        <button
                            key={s}
                            type="button"
                            onClick={() => setScope(s)}
                            className={`rounded-full px-3 py-0.5 text-xs capitalize transition-colors ${
                                scope === s
                                    ? "bg-primary text-white"
                                    : "border border-border bg-bg-secondary text-text-muted hover:text-text"
                            }`}
                        >
                            {s === "personal" ? "My docs" : s}
                        </button>
                    ))}
                    <input
                        type="text"
                        value={query}
                        onChange={(e) => setQuery(e.target.value)}
                        placeholder="Search title or tag…"
                        className="ml-2 flex-1 rounded border border-border bg-bg-secondary px-2 py-1 text-sm text-text"
                    />
                </div>

                <div className="flex-1 overflow-y-auto px-2 py-2">
                    {isLoading ? (
                        <p className="px-2 py-4 text-sm text-text-subtle">
                            Loading…
                        </p>
                    ) : filtered.length === 0 ? (
                        <p className="px-2 py-4 text-sm text-text-subtle">
                            No documents match.
                        </p>
                    ) : (
                        <ul className="divide-y divide-border-light">
                            {filtered.map((d) => {
                                const sel = picked.has(d.id);
                                return (
                                    <li key={d.id}>
                                        <label
                                            className={`flex cursor-pointer items-start gap-3 rounded px-2 py-2 hover:bg-card-hover ${
                                                sel ? "bg-primary-soft/40" : ""
                                            }`}
                                        >
                                            <input
                                                type="checkbox"
                                                checked={sel}
                                                onChange={() => toggle(d)}
                                                className="mt-0.5 h-4 w-4 cursor-pointer accent-primary"
                                            />
                                            <div className="min-w-0 flex-1">
                                                <div className="truncate text-sm font-medium text-text">
                                                    {d.title || "(untitled)"}
                                                </div>
                                                <div className="mt-0.5 flex flex-wrap gap-1 text-xs text-text-muted">
                                                    <span className="capitalize">
                                                        {d.source_type ||
                                                            "editor"}
                                                    </span>
                                                    {d.owner_user_id && (
                                                        <span>· personal</span>
                                                    )}
                                                    {(d.tags || []).map((t) => (
                                                        <span
                                                            key={t}
                                                            className="rounded bg-bg-secondary px-1"
                                                        >
                                                            #{t}
                                                        </span>
                                                    ))}
                                                </div>
                                            </div>
                                        </label>
                                    </li>
                                );
                            })}
                        </ul>
                    )}
                </div>

                <footer className="flex items-center justify-between gap-2 border-t border-border px-4 py-3">
                    <span className="text-xs text-text-muted">
                        {picked.size} selected
                    </span>
                    <div className="flex gap-2">
                        <button
                            type="button"
                            onClick={onClose}
                            className="rounded border border-border bg-bg-secondary px-3 py-1.5 text-sm hover:bg-card-hover"
                        >
                            Cancel
                        </button>
                        <button
                            type="button"
                            disabled={picked.size === 0}
                            onClick={() =>
                                onConfirm(Array.from(picked.values()))
                            }
                            className="rounded bg-primary px-3 py-1.5 text-sm font-medium text-white hover:bg-primary-hover disabled:opacity-50"
                        >
                            Attach {picked.size > 0 ? `(${picked.size})` : ""}
                        </button>
                    </div>
                </footer>
            </div>
        </>
    );
}