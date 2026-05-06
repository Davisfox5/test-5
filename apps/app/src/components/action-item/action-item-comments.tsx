"use client";

import { useState } from "react";
import {
    useActionItemComments,
    useAddActionItemComment,
} from "@/lib/action-items";

export function ActionItemComments({
    actionItemId,
}: {
    actionItemId: string;
}) {
    const { data: comments = [], isLoading } = useActionItemComments(actionItemId);
    const add = useAddActionItemComment();
    const [draft, setDraft] = useState("");

    return (
        <div className="space-y-2">
            {isLoading ? (
                <div className="text-xs text-text-muted">Loading…</div>
            ) : comments.length === 0 ? (
                <div className="text-xs text-text-muted">No discussion yet.</div>
            ) : (
                <ul className="space-y-2">
                    {comments.map((c) => (
                        <li
                            key={c.id}
                            className="rounded border border-border-light bg-bg-secondary p-2 text-sm text-text"
                        >
                            <div className="mb-0.5 text-xs text-text-muted">
                                {new Date(c.created_at).toLocaleString()}
                            </div>
                            <div className="whitespace-pre-wrap">{c.body}</div>
                        </li>
                    ))}
                </ul>
            )}
            <form
                onSubmit={(e) => {
                    e.preventDefault();
                    const body = draft.trim();
                    if (!body) return;
                    add.mutate(
                        { id: actionItemId, body },
                        { onSuccess: () => setDraft("") },
                    );
                }}
                className="flex items-start gap-2"
            >
                <textarea
                    value={draft}
                    onChange={(e) => setDraft(e.target.value)}
                    placeholder="Add a comment…"
                    rows={2}
                    className="flex-1 rounded border border-border bg-card px-2 py-1 text-sm text-text"
                />
                <button
                    type="submit"
                    disabled={!draft.trim() || add.isPending}
                    className="self-end rounded bg-primary px-3 py-1 text-sm font-medium text-white hover:bg-primary-hover disabled:opacity-50"
                >
                    Post
                </button>
            </form>
        </div>
    );
}
