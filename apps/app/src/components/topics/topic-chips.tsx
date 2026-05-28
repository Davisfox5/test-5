"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { useApi } from "@/lib/api";
import { useContextDrawer } from "@/components/context-drawer/context-drawer";

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
export function TopicChips({ topics }: { topics: Topic[] | undefined }) {
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
                                        body: <TopicDrawerContent topic={t} />,
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

function TopicDrawerContent({ topic }: { topic: Topic }) {
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

            {/* Action item linker. */}
            <section className="rounded border border-border bg-bg-card p-3">
                <h4 className="text-xs font-semibold uppercase tracking-wide text-text-muted">
                    Take action
                </h4>
                <p className="mt-1 text-xs text-text-muted">
                    Create a follow-up tied to this topic. The action items
                    page lets you author one with this topic name pre-filled
                    in the title.
                </p>
                <Link
                    href={`/action-items?new=1&topic=${encodeURIComponent(
                        topic.name,
                    )}`}
                    className="mt-2 inline-block rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-white hover:bg-primary-hover"
                >
                    + Create action item
                </Link>
            </section>
        </div>
    );
}
