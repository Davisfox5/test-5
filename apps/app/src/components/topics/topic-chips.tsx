"use client";

import { useState } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { useApi } from "@/lib/api";
import { useContextDrawer } from "@/components/context-drawer/context-drawer";
import { useCreateActionItem } from "@/lib/action-items";

interface Topic {
    name: string;
    relevance?: number;
    mentions?: number;
}

interface KBHit {
    chunk_id: string;
    doc_id: string;
    text: string;
    doc_title: string | null;
    source_url: string | null;
}

interface RelatedInteraction {
    id: string;
    title: string | null;
    channel: string;
    created_at: string;
}

/**
 * Topics. Horizontal bar chart sorted by mention count.
 *
 * Each row shows the topic name, a proportional bar, and the mention
 * count. Clicking a row opens the ContextDrawer with topic-scoped
 * detail (KB articles, related calls, action-item creation).
 */
export function TopicChips({
    topics,
    interactionId,
}: {
    topics: Topic[] | undefined;
    /** When set, the topic drawer's "Create action item" form will
     * attach the new item to this interaction. Required because
     * action_items.interaction_id is a NOT NULL FK on the backend. */
    interactionId?: string;
}) {
    const drawer = useContextDrawer();
    if (!topics || topics.length === 0) return null;

    // Sort descending by mention count; ties broken by relevance.
    const sorted = [...topics].sort((a, b) => {
        const ma = a.mentions ?? 0;
        const mb = b.mentions ?? 0;
        if (mb !== ma) return mb - ma;
        return (b.relevance ?? 0) - (a.relevance ?? 0);
    });
    const max = Math.max(...sorted.map((t) => t.mentions ?? 1), 1);

    return (
        <section className="rounded-lg border border-border bg-bg-card p-4">
            <header className="mb-3 flex items-baseline justify-between">
                <h3 className="text-sm font-semibold">Topics</h3>
                <span className="text-xs text-text-subtle">
                    Sorted by mentions. Click for related calls and KB hits.
                </span>
            </header>
            <ul className="space-y-1.5">
                {sorted.map((t) => {
                    const mentions = t.mentions ?? 1;
                    const widthPct = Math.max(
                        4,
                        Math.round((mentions / max) * 100),
                    );
                    return (
                        <li key={t.name}>
                            <button
                                type="button"
                                onClick={() =>
                                    drawer.open({
                                        title: `Topic: ${t.name}`,
                                        body: (
                                            <TopicDrawerContent
                                                topic={t}
                                                interactionId={interactionId}
                                            />
                                        ),
                                    })
                                }
                                className="group grid w-full grid-cols-[minmax(0,1fr)_3rem] items-center gap-3 rounded px-2 py-1 text-left hover:bg-card-hover focus:outline-none focus:ring-2 focus:ring-primary"
                                title={`${mentions} mentions${
                                    t.relevance != null
                                        ? ` (relevance ${(t.relevance * 100).toFixed(0)}%)`
                                        : ""
                                }`}
                            >
                                <div className="min-w-0">
                                    <div className="truncate text-sm text-text">
                                        {t.name}
                                    </div>
                                    <div
                                        aria-hidden
                                        className="mt-1 h-1.5 rounded-full bg-primary/15"
                                    >
                                        <div
                                            className="h-full rounded-full bg-primary group-hover:bg-primary-hover"
                                            style={{ width: `${widthPct}%` }}
                                        />
                                    </div>
                                </div>
                                <div className="text-right text-xs text-text-muted">
                                    {mentions}
                                </div>
                            </button>
                        </li>
                    );
                })}
            </ul>
        </section>
    );
}

function TopicDrawerContent({
    topic,
    interactionId,
}: {
    topic: Topic;
    interactionId?: string;
}) {
    const api = useApi();
    const kb = useQuery({
        queryKey: ["topic-kb", topic.name],
        queryFn: () =>
            api.get<KBHit[]>(
                `/kb/search?query=${encodeURIComponent(topic.name)}&limit=5`,
            ),
        // KB search hits Voyage / pgvector; cache them so re-opening the
        // drawer for the same topic in the same session is free.
        staleTime: 60_000,
        retry: false,
    });
    const related = useQuery({
        queryKey: ["topic-interactions", topic.name],
        queryFn: () =>
            api.get<RelatedInteraction[]>(
                `/interactions?q=${encodeURIComponent(topic.name)}&limit=5`,
            ),
        staleTime: 60_000,
        retry: false,
    });

    return (
        <div className="space-y-4 text-sm">
            <div className="flex gap-6">
                <div>
                    <div className="text-xs font-semibold uppercase tracking-wide text-text-muted">
                        Mentions
                    </div>
                    <div className="text-text">{topic.mentions ?? 1}</div>
                </div>
                {topic.relevance != null && (
                    <div>
                        <div
                            className="text-xs font-semibold uppercase tracking-wide text-text-muted"
                            title="How central this topic was to the call's main thread. 100% means the call was about this; 30% means it was mentioned but not central."
                        >
                            Relevance
                        </div>
                        <div className="text-text">
                            {(topic.relevance * 100).toFixed(0)}%
                        </div>
                        <div className="mt-0.5 text-[10px] text-text-subtle">
                            of the call&apos;s main thread
                        </div>
                    </div>
                )}
            </div>

            {/* KB articles. */}
            <section>
                <h4 className="text-xs font-semibold uppercase tracking-wide text-text-muted">
                    Knowledge base
                </h4>
                {kb.isLoading ? (
                    <p className="mt-1 text-xs text-text-subtle">Searching…</p>
                ) : !kb.data || kb.data.length === 0 ? (
                    <p className="mt-1 text-xs text-text-subtle">
                        No KB articles match this topic yet.
                    </p>
                ) : (
                    <ul className="mt-1 space-y-2">
                        {kb.data.map((h) => (
                            <li
                                key={h.chunk_id}
                                className="rounded border border-border-light bg-bg-secondary px-3 py-2"
                            >
                                <div className="font-medium text-text">
                                    {h.doc_title || "(untitled)"}
                                </div>
                                <p className="mt-1 line-clamp-2 text-xs text-text-muted">
                                    {h.text}
                                </p>
                                <Link
                                    href={`/knowledge-base#${h.doc_id}`}
                                    className="mt-1 inline-block text-xs text-primary hover:underline"
                                >
                                    Open doc →
                                </Link>
                            </li>
                        ))}
                    </ul>
                )}
            </section>

            {/* Related calls. */}
            <section>
                <h4 className="text-xs font-semibold uppercase tracking-wide text-text-muted">
                    Related calls
                </h4>
                {related.isLoading ? (
                    <p className="mt-1 text-xs text-text-subtle">Loading…</p>
                ) : !related.data || related.data.length === 0 ? (
                    <p className="mt-1 text-xs text-text-subtle">
                        No other calls reference this topic.
                    </p>
                ) : (
                    <ul className="mt-1 space-y-1">
                        {related.data.map((r) => (
                            <li key={r.id}>
                                <Link
                                    href={`/interactions/${r.id}`}
                                    className="block rounded px-2 py-1 text-xs text-text hover:bg-card-hover"
                                >
                                    <span className="font-medium">
                                        {r.title || "(untitled call)"}
                                    </span>
                                    <span className="ml-2 text-text-subtle">
                                        {new Date(
                                            r.created_at,
                                        ).toLocaleDateString()}
                                    </span>
                                </Link>
                            </li>
                        ))}
                    </ul>
                )}
            </section>

            {/* Inline action-item creator. */}
            {interactionId ? (
                <CreateActionItemFromTopic
                    topic={topic}
                    interactionId={interactionId}
                />
            ) : (
                <p className="text-xs text-text-subtle">
                    Open this topic from an interaction page to create an
                    action item scoped to that call.
                </p>
            )}

            {/* Fallback: open the full action-items page if the rep wants
                richer fields than the inline form exposes. */}
            <Link
                href="/action-plans"
                className="mt-1 block text-xs text-text-subtle hover:text-text-muted"
            >
                Browse all action plans →
            </Link>
        </div>
    );
}

/**
 * Inline action-item creator scoped to a topic.
 *
 * Pre-fills the title from the topic name + a friendly verb, lets the
 * rep edit it, and creates the item in place. Replaces the old
 * "redirect to /action-items?new=1&topic=..." pattern that just sent
 * the user away with a query string and left them to fill the form
 * from scratch.
 */
function CreateActionItemFromTopic({
    topic,
    interactionId,
}: {
    topic: Topic;
    interactionId: string;
}) {
    const create = useCreateActionItem();
    const [title, setTitle] = useState(`Follow up on ${topic.name}`);
    const [description, setDescription] = useState(
        `Customer brought up ${topic.name} on the call (${topic.mentions ?? 1} ${topic.mentions === 1 ? "mention" : "mentions"}). Decide next-step ownership.`,
    );
    const [priority, setPriority] = useState<"high" | "medium" | "low">("medium");
    const [createdId, setCreatedId] = useState<string | null>(null);
    const [error, setError] = useState<string | null>(null);

    async function handleCreate() {
        setError(null);
        try {
            const created = await create.mutateAsync({
                interaction_id: interactionId,
                title: title.trim(),
                description: description.trim() || undefined,
                priority,
                category: "follow_up",
            });
            setCreatedId(created.id);
        } catch (err) {
            setError((err as Error).message || "Failed to create action item");
        }
    }

    if (createdId) {
        return (
            <section className="rounded border border-accent-emerald/40 bg-accent-emerald/10 p-3">
                <p className="text-xs text-accent-emerald">
                    Created.{" "}
                    <Link href="/action-items" className="underline">
                        Open Action Items
                    </Link>{" "}
                    to assign or edit.
                </p>
            </section>
        );
    }

    return (
        <section className="space-y-2 rounded border border-border bg-bg-card p-3">
            <h4 className="text-xs font-semibold uppercase tracking-wide text-text-muted">
                Create action item
            </h4>
            <label className="block text-xs">
                <span className="text-text-subtle">Title</span>
                <input
                    type="text"
                    value={title}
                    onChange={(e) => setTitle(e.target.value)}
                    className="mt-0.5 w-full rounded border border-border bg-bg-card px-2 py-1 text-sm text-text"
                />
            </label>
            <label className="block text-xs">
                <span className="text-text-subtle">Description</span>
                <textarea
                    value={description}
                    onChange={(e) => setDescription(e.target.value)}
                    rows={2}
                    className="mt-0.5 w-full rounded border border-border bg-bg-card px-2 py-1 text-sm text-text"
                />
            </label>
            <label className="block text-xs">
                <span className="text-text-subtle">Priority</span>
                <select
                    value={priority}
                    onChange={(e) => setPriority(e.target.value as typeof priority)}
                    className="mt-0.5 w-full rounded border border-border bg-bg-card px-2 py-1 text-sm text-text"
                >
                    <option value="high">High</option>
                    <option value="medium">Medium</option>
                    <option value="low">Low</option>
                </select>
            </label>
            {error && (
                <p className="text-xs text-accent-rose">{error}</p>
            )}
            <button
                type="button"
                onClick={handleCreate}
                disabled={create.isPending || !title.trim()}
                className="rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-white hover:bg-primary-hover disabled:opacity-50"
            >
                {create.isPending ? "Creating…" : "Create"}
            </button>
        </section>
    );
}
