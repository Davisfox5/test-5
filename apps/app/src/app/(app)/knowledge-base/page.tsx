"use client";

import Link from "next/link";
import { useMemo, useState } from "react";

import {
    KBDoc,
    useKBDocs,
} from "@/lib/knowledge-base";
import { useMe } from "@/lib/me";
import { ManagerGate } from "@/components/admin/section";

const SOURCE_TYPES: { value: string; label: string }[] = [
    { value: "", label: "All sources" },
    { value: "editor", label: "Editor" },
    { value: "upload", label: "Upload" },
    { value: "confluence", label: "Confluence" },
    { value: "notion", label: "Notion" },
    { value: "gdrive", label: "Google Drive" },
    { value: "onedrive", label: "OneDrive" },
    { value: "sharepoint", label: "SharePoint" },
];

export default function KnowledgeBasePage() {
    const { data: me } = useMe();
    const role = me?.user?.role;

    const [q, setQ] = useState("");
    const [tag, setTag] = useState("");
    const [sourceType, setSourceType] = useState("");

    // Tag filter on the backend takes a comma-separated string. Trim
    // and skip empties so we don't ship `tags=` and 422 the request.
    const params = useMemo(
        () => ({
            q: q.trim() || undefined,
            tags: tag.trim() || undefined,
            source_type: sourceType || undefined,
            limit: 200,
        }),
        [q, tag, sourceType],
    );

    const { data, isLoading, error } = useKBDocs(params);
    const isManagerPlus = role === "admin" || role === "manager";

    return (
        <div className="space-y-6">
            <header className="flex items-start justify-between gap-4 flex-wrap">
                <div>
                    <h2 className="text-2xl font-bold">Knowledge base</h2>
                    <p className="text-text-muted mt-1">
                        Documents Linda consults when answering questions and
                        building call briefs.
                    </p>
                </div>
                {isManagerPlus ? (
                    <Link
                        href="/knowledge-base/new"
                        className="rounded-md bg-primary text-white px-3 py-1.5 text-sm font-medium hover:bg-primary/90"
                    >
                        + New doc
                    </Link>
                ) : null}
            </header>

            <ManagerGate role={role}>
                <section className="rounded-lg border border-border bg-bg-card p-4 flex flex-wrap gap-3 items-end">
                    <label className="flex-1 min-w-[200px] text-xs font-medium text-text-muted">
                        Search
                        <input
                            type="search"
                            value={q}
                            onChange={(e) => setQ(e.target.value)}
                            placeholder="Title or body…"
                            className="mt-1 w-full rounded-md border border-border bg-bg-raised px-3 py-2 text-sm"
                        />
                    </label>
                    <label className="flex-1 min-w-[160px] text-xs font-medium text-text-muted">
                        Tag
                        <input
                            type="text"
                            value={tag}
                            onChange={(e) => setTag(e.target.value)}
                            placeholder="e.g. onboarding"
                            className="mt-1 w-full rounded-md border border-border bg-bg-raised px-3 py-2 text-sm"
                        />
                    </label>
                    <label className="flex-1 min-w-[160px] text-xs font-medium text-text-muted">
                        Source type
                        <select
                            value={sourceType}
                            onChange={(e) => setSourceType(e.target.value)}
                            className="mt-1 w-full rounded-md border border-border bg-bg-raised px-3 py-2 text-sm"
                        >
                            {SOURCE_TYPES.map((s) => (
                                <option key={s.value} value={s.value}>
                                    {s.label}
                                </option>
                            ))}
                        </select>
                    </label>
                </section>

                {error ? (
                    <div className="rounded-lg border border-accent-rose bg-bg-card p-4 text-sm text-accent-rose">
                        Couldn&apos;t load documents:{" "}
                        {(error as Error).message}
                    </div>
                ) : isLoading ? (
                    <Skeleton />
                ) : !data || data.length === 0 ? (
                    <EmptyState canCreate={isManagerPlus} />
                ) : (
                    <section className="rounded-lg border border-border bg-bg-card overflow-hidden">
                        <table className="w-full text-sm">
                            <thead className="bg-bg-secondary text-text-subtle text-xs uppercase tracking-wide">
                                <tr>
                                    <th className="px-4 py-2 text-left">
                                        Title
                                    </th>
                                    <th className="px-4 py-2 text-left">
                                        Source
                                    </th>
                                    <th className="px-4 py-2 text-left">
                                        Tags
                                    </th>
                                    <th className="px-4 py-2 text-left">
                                        Last synced
                                    </th>
                                    <th className="px-4 py-2 text-left">
                                        Created
                                    </th>
                                </tr>
                            </thead>
                            <tbody>
                                {data.map((doc) => (
                                    <DocRow key={doc.id} doc={doc} />
                                ))}
                            </tbody>
                        </table>
                    </section>
                )}
            </ManagerGate>
        </div>
    );
}

function DocRow({ doc }: { doc: KBDoc }) {
    return (
        <tr className="border-t border-border hover:bg-bg-secondary">
            <td className="px-4 py-3 font-medium">
                <Link
                    href={`/knowledge-base/${doc.id}`}
                    className="hover:underline"
                >
                    {doc.title || <em className="text-text-subtle">untitled</em>}
                </Link>
            </td>
            <td className="px-4 py-3 text-text-muted capitalize">
                {doc.source_type ?? "—"}
            </td>
            <td className="px-4 py-3 text-text-muted">
                {doc.tags?.length ? (
                    <span className="flex flex-wrap gap-1">
                        {doc.tags.slice(0, 4).map((t) => (
                            <span
                                key={t}
                                className="rounded-md border border-border bg-bg-raised px-1.5 py-0.5 text-xs"
                            >
                                {t}
                            </span>
                        ))}
                        {doc.tags.length > 4 ? (
                            <span className="text-xs text-text-subtle">
                                +{doc.tags.length - 4}
                            </span>
                        ) : null}
                    </span>
                ) : (
                    "—"
                )}
            </td>
            <td className="px-4 py-3 text-text-muted text-xs">
                {doc.last_synced_at
                    ? new Date(doc.last_synced_at).toLocaleString()
                    : "—"}
            </td>
            <td className="px-4 py-3 text-text-muted text-xs">
                {new Date(doc.created_at).toLocaleDateString()}
            </td>
        </tr>
    );
}

function Skeleton() {
    return (
        <div className="rounded-lg border border-border bg-bg-card p-6 animate-pulse">
            <div className="h-4 w-40 bg-bg-secondary rounded mb-3" />
            <div className="h-3 w-2/3 bg-bg-secondary rounded mb-2" />
            <div className="h-3 w-1/2 bg-bg-secondary rounded" />
        </div>
    );
}

function EmptyState({ canCreate }: { canCreate: boolean }) {
    return (
        <div className="rounded-lg border border-border border-dashed bg-bg-card p-10 text-center space-y-4">
            <p className="text-text-muted">
                No documents match your filters yet. Add a doc to give Linda
                more context for your team&apos;s calls.
            </p>
            {canCreate ? (
                <Link
                    href="/knowledge-base/new"
                    className="inline-flex rounded-md bg-primary text-white px-3 py-1.5 text-sm font-medium hover:bg-primary/90"
                >
                    + New doc
                </Link>
            ) : null}
        </div>
    );
}
