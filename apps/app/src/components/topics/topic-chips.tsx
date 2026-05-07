"use client";

import { useContextDrawer } from "@/components/context-drawer/context-drawer";

interface Topic {
    name: string;
    relevance?: number;
    mentions?: number;
}

/**
 * Topic chips — sized circles per topic, sized by mention count.
 *
 * Click a chip to open the ContextDrawer with topic-scoped detail
 * (KB articles, related calls, "create custom action item linked
 * to this topic"). When the topic data is sparse the chip falls back
 * to a min-size pill so it stays readable.
 */
export function TopicChips({ topics }: { topics: Topic[] | undefined }) {
    const drawer = useContextDrawer();
    if (!topics || topics.length === 0) return null;

    const max = Math.max(...topics.map((t) => t.mentions ?? 1));

    return (
        <section className="rounded-lg border border-border bg-bg-card p-4">
            <header className="mb-2 flex items-baseline justify-between">
                <h3 className="text-sm font-semibold">Topics</h3>
                <span className="text-xs text-text-subtle">
                    Sized by mentions · click for KB + related calls
                </span>
            </header>
            <div className="flex flex-wrap items-center gap-2">
                {topics.map((t) => {
                    const ratio = (t.mentions ?? 1) / Math.max(1, max);
                    const size = 28 + ratio * 36; // px
                    return (
                        <button
                            key={t.name}
                            type="button"
                            onClick={() =>
                                drawer.open({
                                    title: `Topic: ${t.name}`,
                                    body: <TopicDrawerContent topic={t} />,
                                })
                            }
                            className="inline-flex flex-col items-center gap-1 rounded-md p-1 hover:bg-card-hover focus:outline-none focus:ring-2 focus:ring-primary"
                            title={`${t.mentions ?? 1} mentions`}
                        >
                            <span
                                aria-hidden
                                className="flex items-center justify-center rounded-full bg-primary-soft text-primary"
                                style={{
                                    width: `${size}px`,
                                    height: `${size}px`,
                                    fontSize: `${10 + ratio * 4}px`,
                                }}
                            >
                                {t.mentions ?? 1}
                            </span>
                            <span className="max-w-[100px] truncate text-xs text-text-muted">
                                {t.name}
                            </span>
                        </button>
                    );
                })}
            </div>
        </section>
    );
}

function TopicDrawerContent({ topic }: { topic: Topic }) {
    return (
        <div className="space-y-3 text-sm">
            <div>
                <div className="text-xs font-semibold uppercase tracking-wide text-text-muted">
                    Mentions
                </div>
                <div className="text-text">{topic.mentions ?? 1}</div>
            </div>
            {topic.relevance != null && (
                <div>
                    <div className="text-xs font-semibold uppercase tracking-wide text-text-muted">
                        Relevance
                    </div>
                    <div className="text-text">
                        {(topic.relevance * 100).toFixed(0)}%
                    </div>
                </div>
            )}
            <div className="rounded border border-border-light bg-bg-secondary p-2 text-xs text-text-muted">
                Related KB articles, prior calls referencing this topic, and a
                &quot;create custom action item linked to this topic&quot; flow
                slot in here. The drawer is the chip&apos;s landing surface;
                content gets richer as those data sources wire up.
            </div>
        </div>
    );
}
