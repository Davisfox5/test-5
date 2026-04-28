"use client";

import Link from "next/link";
import { useState } from "react";

import {
    ScorecardTemplate,
    totalWeight,
    useDeleteScorecard,
    useScorecards,
} from "@/lib/scorecards";

export default function ScorecardsPage() {
    const { data, isLoading, error } = useScorecards();
    const del = useDeleteScorecard();
    const [pendingDelete, setPendingDelete] = useState<ScorecardTemplate | null>(null);

    return (
        <div className="space-y-6">
            <header className="flex items-start justify-between gap-4 flex-wrap">
                <div>
                    <h2 className="text-2xl font-bold">Scorecards</h2>
                    <p className="text-text-muted mt-1">
                        Templates Linda uses to grade your calls.
                    </p>
                </div>
                <Link
                    href="/scorecards/new"
                    className="rounded-md bg-primary text-white px-3 py-1.5 text-sm font-medium hover:bg-primary/90"
                >
                    + Create scorecard
                </Link>
            </header>

            {error ? (
                <div className="rounded-lg border border-accent-rose bg-bg-card p-4 text-sm text-accent-rose">
                    Couldn&apos;t load scorecards: {(error as Error).message}
                </div>
            ) : isLoading ? (
                <Skeleton />
            ) : !data || data.length === 0 ? (
                <EmptyState />
            ) : (
                <section className="rounded-lg border border-border bg-bg-card overflow-hidden">
                    <table className="w-full text-sm">
                        <thead className="bg-bg-secondary text-text-subtle text-xs uppercase tracking-wide">
                            <tr>
                                <th className="px-4 py-2 text-left">Name</th>
                                <th className="px-4 py-2 text-left">Rubric items</th>
                                <th className="px-4 py-2 text-left">Total weight</th>
                                <th className="px-4 py-2 text-left">Last edited</th>
                                <th className="px-4 py-2 text-right">Actions</th>
                            </tr>
                        </thead>
                        <tbody>
                            {data.map((tpl) => (
                                <tr
                                    key={tpl.id}
                                    className="border-t border-border hover:bg-bg-secondary"
                                >
                                    <td className="px-4 py-3 font-medium">
                                        <Link
                                            href={`/scorecards/${tpl.id}`}
                                            className="hover:underline"
                                        >
                                            {tpl.name}
                                            {tpl.is_default ? (
                                                <span className="ml-2 text-xs text-text-subtle">
                                                    (default)
                                                </span>
                                            ) : null}
                                        </Link>
                                    </td>
                                    <td className="px-4 py-3 text-text-muted">
                                        {tpl.criteria.length}
                                    </td>
                                    <td className="px-4 py-3 text-text-muted">
                                        {totalWeight(tpl.criteria)}
                                    </td>
                                    <td className="px-4 py-3 text-text-muted">
                                        {new Date(tpl.created_at).toLocaleDateString()}
                                    </td>
                                    <td className="px-4 py-3 text-right">
                                        <button
                                            onClick={() => setPendingDelete(tpl)}
                                            className="rounded-md border border-border px-2 py-1 text-xs text-accent-rose hover:bg-bg-secondary"
                                        >
                                            Delete
                                        </button>
                                    </td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                </section>
            )}

            {pendingDelete ? (
                <ConfirmDialog
                    title={`Delete "${pendingDelete.name}"?`}
                    body="This is permanent. Existing scores stay; future calls won't be graded against this template."
                    onCancel={() => setPendingDelete(null)}
                    confirmLabel={del.isPending ? "Deleting…" : "Delete"}
                    onConfirm={() => {
                        del.mutate(pendingDelete.id, {
                            onSettled: () => setPendingDelete(null),
                        });
                    }}
                />
            ) : null}
        </div>
    );
}

function ConfirmDialog({
    title,
    body,
    onCancel,
    onConfirm,
    confirmLabel,
}: {
    title: string;
    body: string;
    onCancel: () => void;
    onConfirm: () => void;
    confirmLabel: string;
}) {
    return (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
            <div className="w-full max-w-md rounded-lg border border-border bg-bg-card p-5 shadow-lg">
                <h3 className="text-lg font-semibold">{title}</h3>
                <p className="mt-2 text-sm text-text-muted">{body}</p>
                <div className="mt-5 flex justify-end gap-2">
                    <button
                        onClick={onCancel}
                        className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-bg-secondary"
                    >
                        Cancel
                    </button>
                    <button
                        onClick={onConfirm}
                        className="rounded-md bg-accent-rose text-white px-3 py-1.5 text-sm hover:opacity-90"
                    >
                        {confirmLabel}
                    </button>
                </div>
            </div>
        </div>
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

function EmptyState() {
    return (
        <div className="rounded-lg border border-border border-dashed bg-bg-card p-10 text-center space-y-4">
            <p className="text-text-muted">
                Scorecards let Linda grade calls against your team&apos;s rubric.
                Create one to get started.
            </p>
            <Link
                href="/scorecards/new"
                className="inline-flex rounded-md bg-primary text-white px-3 py-1.5 text-sm font-medium hover:bg-primary/90"
            >
                + Create scorecard
            </Link>
        </div>
    );
}
