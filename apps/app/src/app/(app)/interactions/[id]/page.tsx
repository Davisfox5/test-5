"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useApi } from "@/lib/api";
import {
    formatDuration,
    formatRelative,
    sentimentLabel,
    useDeleteInteraction,
    useInteraction,
    useUpdateInteraction,
    type ActionItemOut,
    type TranscriptTurn,
} from "@/lib/interactions";

export default function InteractionDetailPage() {
    const params = useParams<{ id: string }>();
    const id = params?.id;
    const router = useRouter();
    const detail = useInteraction(id);
    const update = useUpdateInteraction();
    const del = useDeleteInteraction();

    const [editing, setEditing] = useState(false);
    const [titleDraft, setTitleDraft] = useState("");
    const [confirmingDelete, setConfirmingDelete] = useState(false);

    useEffect(() => {
        if (detail.data && !editing) {
            setTitleDraft(detail.data.title ?? "");
        }
    }, [detail.data, editing]);

    if (!id) return null;

    if (detail.isLoading) {
        return (
            <div className="space-y-4">
                <div className="h-6 w-1/3 animate-pulse rounded bg-bg-card-hover" />
                <div className="h-4 w-1/4 animate-pulse rounded bg-bg-card-hover" />
                <div className="h-64 animate-pulse rounded-lg bg-bg-card" />
            </div>
        );
    }

    if (detail.error || !detail.data) {
        return (
            <div className="space-y-3">
                <Link
                    href="/interactions"
                    className="text-sm text-primary hover:underline"
                >
                    ← Back to interactions
                </Link>
                <p className="text-accent-rose">
                    Couldn&apos;t load this interaction.
                </p>
            </div>
        );
    }

    const i = detail.data;
    const sent = sentimentLabel(i.insights?.sentiment_score);

    async function handleSaveTitle() {
        if (!id) return;
        try {
            await update.mutateAsync({ id, patch: { title: titleDraft } });
            setEditing(false);
        } catch {
            // surface via update.error below
        }
    }

    async function handleDelete() {
        if (!id) return;
        try {
            await del.mutateAsync(id);
            router.push("/interactions");
        } catch {
            setConfirmingDelete(false);
        }
    }

    return (
        <div className="space-y-6">
            <div>
                <Link
                    href="/interactions"
                    className="text-sm text-primary hover:underline"
                >
                    ← Back to interactions
                </Link>
            </div>

            <header className="rounded-lg border border-border bg-bg-card p-5">
                <div className="flex flex-wrap items-start justify-between gap-4">
                    <div className="min-w-0 flex-1">
                        {editing ? (
                            <div className="flex items-center gap-2">
                                <input
                                    type="text"
                                    value={titleDraft}
                                    onChange={(e) =>
                                        setTitleDraft(e.target.value)
                                    }
                                    className="w-full rounded-md border border-border bg-bg-secondary px-3 py-2 text-lg font-semibold outline-none focus:border-primary"
                                />
                                <button
                                    type="button"
                                    onClick={handleSaveTitle}
                                    disabled={update.isPending}
                                    className="rounded-md bg-primary px-3 py-2 text-sm font-medium text-white hover:bg-primary-hover disabled:opacity-60"
                                >
                                    Save
                                </button>
                                <button
                                    type="button"
                                    onClick={() => {
                                        setEditing(false);
                                        setTitleDraft(i.title ?? "");
                                    }}
                                    className="rounded-md border border-border px-3 py-2 text-sm text-text-muted hover:bg-bg-card-hover"
                                >
                                    Cancel
                                </button>
                            </div>
                        ) : (
                            <div className="flex items-center gap-2">
                                <h2 className="truncate text-2xl font-bold">
                                    {i.title ||
                                        i.caller_phone ||
                                        "Untitled call"}
                                </h2>
                                <button
                                    type="button"
                                    onClick={() => setEditing(true)}
                                    className="text-xs text-primary hover:underline"
                                >
                                    Edit
                                </button>
                            </div>
                        )}
                        <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-sm text-text-muted">
                            <span>{formatRelative(i.created_at)}</span>
                            <span>•</span>
                            <span>{formatDuration(i.duration_seconds)}</span>
                            <span>•</span>
                            <span className="capitalize">{i.channel}</span>
                            {i.caller_phone ? (
                                <>
                                    <span>•</span>
                                    <span>{i.caller_phone}</span>
                                </>
                            ) : null}
                        </div>
                    </div>
                    <div className="flex shrink-0 items-center gap-3">
                        <span className="rounded-full border border-border px-3 py-1 text-xs capitalize text-text-muted">
                            {i.status}
                        </span>
                    </div>
                </div>
                {update.isError ? (
                    <p className="mt-3 text-sm text-accent-rose">
                        Couldn&apos;t save:{" "}
                        {update.error instanceof Error
                            ? update.error.message
                            : "unknown error"}
                    </p>
                ) : null}
            </header>

            <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
                <section className="rounded-lg border border-border bg-bg-card lg:col-span-2">
                    <div className="border-b border-border px-5 py-3">
                        <h3 className="text-sm font-semibold">Transcript</h3>
                    </div>
                    <div className="max-h-[60vh] overflow-y-auto px-5 py-4">
                        {i.status === "processing" ? (
                            <p className="text-sm text-text-muted">
                                Linda is transcribing this call. The transcript
                                will appear here when it&apos;s ready.
                            </p>
                        ) : i.transcript && i.transcript.length > 0 ? (
                            <TranscriptList turns={i.transcript} />
                        ) : i.raw_text ? (
                            <pre className="whitespace-pre-wrap text-sm text-text-muted">
                                {i.raw_text}
                            </pre>
                        ) : (
                            <p className="text-sm text-text-subtle">
                                No transcript available for this interaction.
                            </p>
                        )}
                    </div>
                </section>

                <aside className="space-y-6">
                    <section className="rounded-lg border border-border bg-bg-card p-5">
                        <h3 className="text-sm font-semibold">Scores</h3>
                        <dl className="mt-3 space-y-2 text-sm">
                            <ScoreRow
                                label="Sentiment"
                                value={
                                    i.insights?.sentiment_score != null
                                        ? `${i.insights.sentiment_score.toFixed(1)} / 10`
                                        : "—"
                                }
                                accent={sent.tone}
                                accentText={sent.text}
                            />
                            <ScoreRow
                                label="Churn risk"
                                value={
                                    i.insights?.churn_risk != null
                                        ? `${(i.insights.churn_risk * 100).toFixed(0)}%`
                                        : "—"
                                }
                                accentText={
                                    (i.insights?.churn_risk_signal as
                                        | string
                                        | undefined) ?? undefined
                                }
                            />
                            <ScoreRow
                                label="Upsell"
                                value={
                                    i.insights?.upsell_score != null
                                        ? `${(i.insights.upsell_score * 100).toFixed(0)}%`
                                        : "—"
                                }
                                accentText={
                                    (i.insights?.upsell_signal as
                                        | string
                                        | undefined) ?? undefined
                                }
                            />
                            <ScoreRow
                                label="Complexity"
                                value={
                                    i.complexity_score != null
                                        ? i.complexity_score.toFixed(2)
                                        : "—"
                                }
                            />
                            <ScoreRow
                                label="PII redacted"
                                value={i.pii_redacted ? "Yes" : "No"}
                            />
                        </dl>
                        {i.insights?.summary ? (
                            <div className="mt-4 rounded-md border border-border bg-bg-secondary p-3 text-xs text-text-muted">
                                {String(i.insights.summary)}
                            </div>
                        ) : null}
                    </section>

                    <ActionItemsForInteraction interactionId={i.id} />
                </aside>
            </div>

            <section className="flex items-center justify-between rounded-lg border border-border bg-bg-card p-5">
                <div>
                    <h3 className="text-sm font-semibold text-accent-rose">
                        Danger zone
                    </h3>
                    <p className="text-xs text-text-muted">
                        Delete this interaction and its transcript permanently.
                    </p>
                </div>
                {confirmingDelete ? (
                    <div className="flex items-center gap-2">
                        <span className="text-sm text-text-muted">
                            Are you sure?
                        </span>
                        <button
                            type="button"
                            onClick={handleDelete}
                            disabled={del.isPending}
                            className="rounded-md bg-accent-rose px-3 py-2 text-sm font-medium text-white hover:opacity-90 disabled:opacity-60"
                        >
                            {del.isPending ? "Deleting…" : "Yes, delete"}
                        </button>
                        <button
                            type="button"
                            onClick={() => setConfirmingDelete(false)}
                            className="rounded-md border border-border px-3 py-2 text-sm text-text-muted hover:bg-bg-card-hover"
                        >
                            Cancel
                        </button>
                    </div>
                ) : (
                    <button
                        type="button"
                        onClick={() => setConfirmingDelete(true)}
                        className="rounded-md border border-accent-rose/40 px-3 py-2 text-sm font-medium text-accent-rose hover:bg-accent-rose/10"
                    >
                        Delete interaction
                    </button>
                )}
            </section>
        </div>
    );
}

function TranscriptList({ turns }: { turns: TranscriptTurn[] }) {
    return (
        <ol className="space-y-3">
            {turns.map((turn, idx) => {
                const speaker =
                    (turn.speaker as string | undefined) ||
                    (turn.role as string | undefined) ||
                    `Speaker ${idx + 1}`;
                const text = (turn.text as string | undefined) ?? "";
                return (
                    <li key={idx} className="flex gap-3">
                        <div className="w-24 shrink-0 text-xs font-semibold uppercase tracking-wide text-text-subtle">
                            {speaker}
                            {typeof turn.start === "number" ? (
                                <div className="font-normal normal-case text-text-subtle">
                                    {Math.floor(turn.start / 60)}:
                                    {String(
                                        Math.floor(turn.start % 60),
                                    ).padStart(2, "0")}
                                </div>
                            ) : null}
                        </div>
                        <p className="flex-1 text-sm text-text-muted">
                            {text}
                        </p>
                    </li>
                );
            })}
        </ol>
    );
}

function ScoreRow({
    label,
    value,
    accent,
    accentText,
}: {
    label: string;
    value: string;
    accent?: "emerald" | "amber" | "rose" | "subtle";
    accentText?: string;
}) {
    return (
        <div className="flex items-center justify-between border-b border-border py-2 last:border-b-0">
            <dt className="text-text-muted">{label}</dt>
            <dd className="text-right">
                <div className="font-medium">{value}</div>
                {accentText ? (
                    <div
                        className={`text-xs ${
                            accent === "emerald"
                                ? "text-accent-emerald"
                                : accent === "amber"
                                  ? "text-accent-amber"
                                  : accent === "rose"
                                    ? "text-accent-rose"
                                    : "text-text-subtle"
                        }`}
                    >
                        {accentText}
                    </div>
                ) : null}
            </dd>
        </div>
    );
}

function ActionItemsForInteraction({ interactionId }: { interactionId: string }) {
    const api = useApi();
    const items = useQuery({
        queryKey: ["action-items", "by-interaction", interactionId],
        queryFn: async () => {
            // No /interactions/{id}/action-items endpoint — pull a wide
            // window from /action-items and filter client-side. Fine for
            // a single-call detail page; switch to a server filter if
            // tenants ever generate >200 items per call.
            const all = await api.get<ActionItemOut[]>(
                "/action-items?limit=200",
            );
            return all.filter((it) => it.interaction_id === interactionId);
        },
    });

    return (
        <section className="rounded-lg border border-border bg-bg-card p-5">
            <h3 className="text-sm font-semibold">Action items</h3>
            {items.isLoading ? (
                <p className="mt-3 text-sm text-text-subtle">Loading…</p>
            ) : items.error ? (
                <p className="mt-3 text-sm text-accent-rose">
                    Couldn&apos;t load action items.
                </p>
            ) : items.data && items.data.length > 0 ? (
                <ul className="mt-3 space-y-2">
                    {items.data.map((it) => (
                        <li
                            key={it.id}
                            className="rounded-md border border-border bg-bg-secondary p-3"
                        >
                            <div className="flex items-center justify-between gap-2">
                                <span className="text-sm font-medium">
                                    {it.title}
                                </span>
                                <span className="text-xs uppercase tracking-wide text-text-subtle">
                                    {it.priority}
                                </span>
                            </div>
                            {it.description ? (
                                <p className="mt-1 text-xs text-text-muted">
                                    {it.description}
                                </p>
                            ) : null}
                        </li>
                    ))}
                </ul>
            ) : (
                <p className="mt-3 text-sm text-text-subtle">
                    No action items pulled from this interaction yet.
                </p>
            )}
        </section>
    );
}
