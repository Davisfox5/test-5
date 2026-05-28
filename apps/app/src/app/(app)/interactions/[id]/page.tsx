"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useApi } from "@/lib/api";
import {
    formatDuration,
    formatRelative,
    sentimentLabel,
    useDeleteInteraction,
    useInteraction,
    useRedriveInteraction,
    useUpdateInteraction,
    type InlineTag,
    type TranscriptTurn,
} from "@/lib/interactions";
import {
    useFollowUpDraft,
    useSendFollowUp,
    type EmailSendOut,
} from "@/lib/communications";
import { useOAuthStatus } from "@/lib/oauth";
import { type ActionItem } from "@/lib/action-items";
import { ActionItemCard } from "@/components/action-item/action-item-card";
import { MethodologyScorecard } from "@/components/methodology/methodology-scorecard";
import { CallDynamicsChart } from "@/components/call-dynamics/call-dynamics-chart";
import { TopicChips } from "@/components/topics/topic-chips";
import {
    findTagForTurn,
    TaggedTurnText,
} from "@/components/transcript/inline-tag-overlay";
import {
    KBFilePickerModal,
    type KBPickedAttachment,
} from "@/components/kb-picker/kb-file-picker";

type TabKey = "overview" | "transcript" | "coaching" | "compliance";

const TABS: Array<{ key: TabKey; label: string }> = [
    { key: "overview", label: "Overview" },
    { key: "transcript", label: "Transcript" },
    { key: "coaching", label: "Coaching" },
    { key: "compliance", label: "Compliance" },
];

export default function InteractionDetailPage() {
    const params = useParams<{ id: string }>();
    const id = params?.id;
    const router = useRouter();
    const detail = useInteraction(id);
    const update = useUpdateInteraction();
    const del = useDeleteInteraction();
    const redrive = useRedriveInteraction();

    const [editing, setEditing] = useState(false);
    const [titleDraft, setTitleDraft] = useState("");
    const [confirmingDelete, setConfirmingDelete] = useState(false);
    // Tab structure: Overview | Transcript | Coaching | Compliance.
    // Hash drives initial tab so action-item deep-links (#follow-up,
    // #coaching) and email outbox links land on the right pane.
    const [tab, setTab] = useState<TabKey>("overview");
    useEffect(() => {
        if (typeof window === "undefined") return;
        const h = (window.location.hash || "").replace("#", "");
        if (h === "transcript" || h === "coaching" || h === "compliance") {
            setTab(h);
        } else if (h === "follow-up") {
            // The follow-up panel lives on the Overview tab; jump there
            // and let the panel's own scrollIntoView handle the rest.
            setTab("overview");
        }
    }, []);

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

    async function handleRedrive() {
        if (!id) return;
        try {
            await redrive.mutateAsync(id);
        } catch {
            // surfaced inline below
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
                        {(i.status === "failed" ||
                            i.status === "processing" ||
                            i.status === "transcription_failed" ||
                            i.status === "transcription_pending") && (
                            <button
                                type="button"
                                onClick={handleRedrive}
                                disabled={redrive.isPending}
                                title="Re-run analysis. Useful for stuck or failed interactions."
                                className="rounded-md border border-border px-3 py-1 text-xs text-text-muted hover:bg-bg-card-hover disabled:opacity-60"
                            >
                                {redrive.isPending ? "Re-driving…" : "Re-drive"}
                            </button>
                        )}
                    </div>
                </div>
                {redrive.isError ? (
                    <p className="mt-3 text-sm text-accent-rose">
                        Couldn&apos;t re-drive:{" "}
                        {redrive.error instanceof Error
                            ? redrive.error.message
                            : "unknown error"}
                    </p>
                ) : null}
                {update.isError ? (
                    <p className="mt-3 text-sm text-accent-rose">
                        Couldn&apos;t save:{" "}
                        {update.error instanceof Error
                            ? update.error.message
                            : "unknown error"}
                    </p>
                ) : null}
            </header>

            <div
                role="tablist"
                aria-label="Interaction tabs"
                className="flex flex-wrap gap-1 border-b border-border"
            >
                {TABS.map((t) => (
                    <button
                        key={t.key}
                        type="button"
                        role="tab"
                        aria-selected={tab === t.key}
                        onClick={() => setTab(t.key)}
                        className={`-mb-px rounded-t-md border-b-2 px-3 py-2 text-sm font-medium transition-colors ${
                            tab === t.key
                                ? "border-primary text-text"
                                : "border-transparent text-text-muted hover:text-text"
                        }`}
                    >
                        {t.label}
                    </button>
                ))}
            </div>

            {tab === "overview" && (
                <div className="space-y-6">
                    <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
                        <section className="rounded-lg border border-border bg-bg-card p-5 lg:col-span-2">
                            <h3 className="text-sm font-semibold">Summary</h3>
                            {i.insights?.summary ? (
                                <p className="mt-3 text-sm text-text-muted">
                                    {String(i.insights.summary)}
                                </p>
                            ) : (
                                <p className="mt-3 text-sm text-text-subtle">
                                    No summary yet. analysis is still in
                                    progress or wasn&apos;t able to produce one.
                                </p>
                            )}
                        </section>
                        <aside>
                            <section className="rounded-lg border border-border bg-bg-card p-5">
                                <h3 className="text-sm font-semibold">
                                    Scores
                                </h3>
                                <dl className="mt-3 space-y-2 text-sm">
                                    <ScoreRow
                                        label="Sentiment"
                                        value={
                                            i.insights?.sentiment_overall
                                                ? String(
                                                      i.insights
                                                          .sentiment_overall,
                                                  )
                                                : "-"
                                        }
                                        accent={sent.tone}
                                        accentText={sent.text}
                                    />
                                    <ScoreRow
                                        label="Churn risk"
                                        value={
                                            (i.insights?.churn_risk_signal as
                                                | string
                                                | undefined) ?? "-"
                                        }
                                    />
                                    <ScoreRow
                                        label="Upsell"
                                        value={
                                            (i.insights?.upsell_signal as
                                                | string
                                                | undefined) ?? "-"
                                        }
                                    />
                                    <ScoreRow
                                        label="Complexity"
                                        value={
                                            i.complexity_score != null
                                                ? i.complexity_score.toFixed(2)
                                                : "-"
                                        }
                                    />
                                    <ScoreRow
                                        label="PII redacted"
                                        value={i.pii_redacted ? "Yes" : "No"}
                                    />
                                </dl>
                            </section>
                        </aside>
                    </div>

                    <CallDynamicsChart
                        trajectory={i.insights?.sentiment_trajectory}
                        keyMoments={i.insights?.key_moments}
                        durationSeconds={i.duration_seconds}
                    />

                    <TopicChips topics={i.insights?.topics} />

                    <ActionItemsForInteraction interactionId={i.id} />

                    <FollowUpPanel interactionId={i.id} />
                </div>
            )}

            {tab === "transcript" && (
                <section className="rounded-lg border border-border bg-bg-card">
                    <div className="border-b border-border px-5 py-3">
                        <h3 className="text-sm font-semibold">Transcript</h3>
                    </div>
                    <div className="max-h-[75vh] overflow-y-auto px-5 py-4">
                        {i.status === "processing" ? (
                            <p className="text-sm text-text-muted">
                                Linda is transcribing this call. The transcript
                                will appear here when it&apos;s ready.
                            </p>
                        ) : i.transcript && i.transcript.length > 0 ? (
                            <TranscriptList
                                turns={i.transcript}
                                inlineTags={
                                    i.insights?.inline_tags as
                                        | InlineTag[]
                                        | undefined
                                }
                            />
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
            )}

            {tab === "coaching" && <CoachingTab insights={i.insights} />}

            {tab === "compliance" && <ComplianceTab insights={i.insights} />}

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

function TranscriptList({
    turns,
    inlineTags,
}: {
    turns: TranscriptTurn[];
    inlineTags?: InlineTag[];
}) {
    return (
        <ol className="space-y-3">
            {turns.map((turn, idx) => {
                const speaker =
                    (turn.speaker as string | undefined) ||
                    (turn.role as string | undefined) ||
                    `Speaker ${idx + 1}`;
                const text = (turn.text as string | undefined) ?? "";
                const startSec = typeof turn.start === "number" ? turn.start : null;
                const turnTime =
                    startSec !== null
                        ? `${Math.floor(startSec / 60)}:${String(
                              Math.floor(startSec % 60),
                          ).padStart(2, "0")}`
                        : "";
                const tag = findTagForTurn(inlineTags, turnTime);
                return (
                    <li key={idx} className="flex gap-3">
                        <div className="w-24 shrink-0 text-xs font-semibold uppercase tracking-wide text-text-subtle">
                            {speaker}
                            {turnTime ? (
                                <div className="font-normal normal-case text-text-subtle">
                                    {turnTime}
                                </div>
                            ) : null}
                        </div>
                        <p className="flex-1 text-sm text-text-muted">
                            <TaggedTurnText text={text} tag={tag} />
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

type ProviderChoice = "auto" | "google" | "microsoft";

const PROVIDER_LABELS: Record<ProviderChoice, string> = {
    auto: "Auto",
    google: "Gmail",
    microsoft: "Outlook",
};

function FollowUpPanel({ interactionId }: { interactionId: string }) {
    const draft = useFollowUpDraft(interactionId);
    const send = useSendFollowUp(interactionId);
    const oauth = useOAuthStatus();
    const sectionRef = useRef<HTMLElement | null>(null);

    const [collapsed, setCollapsed] = useState(false);
    const [subject, setSubject] = useState("");
    const [body, setBody] = useState("");
    const [recipients, setRecipients] = useState<string[]>([""]);
    const [showCcBcc, setShowCcBcc] = useState(false);
    const [cc, setCc] = useState("");
    const [bcc, setBcc] = useState("");
    const [provider, setProvider] = useState<ProviderChoice>("auto");
    const [lastSent, setLastSent] = useState<EmailSendOut | null>(null);
    const [hydrated, setHydrated] = useState(false);
    const [attachments, setAttachments] = useState<KBPickedAttachment[]>([]);
    const [showPicker, setShowPicker] = useState(false);

    // Hydrate the form from the AI draft once it arrives. Don't keep
    // overwriting the user's edits if they keep editing while the query
    // refetches in the background.
    useEffect(() => {
        if (!draft.data || hydrated) return;
        setSubject(draft.data.draft_subject ?? "");
        setBody(draft.data.draft_body ?? "");
        setRecipients(
            draft.data.suggested_to ? [draft.data.suggested_to] : [""],
        );
        setHydrated(true);
    }, [draft.data, hydrated]);

    // Auto-scroll into view when the URL hash is #follow-up — used by
    // the action-items page deep link.
    useEffect(() => {
        if (typeof window === "undefined") return;
        if (window.location.hash === "#follow-up" && sectionRef.current) {
            sectionRef.current.scrollIntoView({ behavior: "smooth" });
        }
    }, [draft.data]);

    const integrations = oauth.data?.integrations ?? [];
    const hasGoogle = integrations.some((i) => i.provider === "google");
    const hasMicrosoft = integrations.some((i) => i.provider === "microsoft");
    const hasAnyProvider = hasGoogle || hasMicrosoft;

    if (collapsed) {
        return (
            <section
                id="follow-up"
                ref={sectionRef}
                className="rounded-lg border border-border bg-bg-card p-5"
            >
                <div className="flex items-center justify-between">
                    <h3 className="text-sm font-semibold">Follow-up email</h3>
                    <button
                        type="button"
                        onClick={() => setCollapsed(false)}
                        className="text-xs text-primary hover:underline"
                    >
                        Show draft
                    </button>
                </div>
            </section>
        );
    }

    if (lastSent) {
        return (
            <section
                id="follow-up"
                ref={sectionRef}
                className="rounded-lg border border-accent-emerald/40 bg-bg-card p-5"
            >
                <div className="flex items-start justify-between gap-4">
                    <div>
                        <h3 className="text-sm font-semibold text-accent-emerald">
                            Follow-up sent
                        </h3>
                        <p className="mt-1 text-xs text-text-muted">
                            via{" "}
                            {PROVIDER_LABELS[
                                (lastSent.provider as ProviderChoice) ?? "auto"
                            ] ?? lastSent.provider}{" "}
                            • {lastSent.to_address} •{" "}
                            {lastSent.sent_at
                                ? new Date(lastSent.sent_at).toLocaleString()
                                : "just now"}
                        </p>
                        <p className="mt-1 text-xs text-text-subtle font-mono">
                            #{lastSent.id.slice(0, 8)}
                        </p>
                    </div>
                    <button
                        type="button"
                        onClick={() => {
                            setLastSent(null);
                            setHydrated(false);
                            draft.refetch();
                        }}
                        className="rounded-md border border-border px-3 py-1.5 text-xs hover:bg-bg-secondary"
                    >
                        Compose another
                    </button>
                </div>
            </section>
        );
    }

    if (draft.isLoading) {
        return (
            <section
                id="follow-up"
                ref={sectionRef}
                className="rounded-lg border border-border bg-bg-card p-5"
            >
                <h3 className="text-sm font-semibold">Follow-up email</h3>
                <p className="mt-2 text-sm text-text-muted">
                    Loading the AI draft…
                </p>
            </section>
        );
    }

    if (draft.error) {
        return (
            <section
                id="follow-up"
                ref={sectionRef}
                className="rounded-lg border border-border bg-bg-card p-5"
            >
                <h3 className="text-sm font-semibold">Follow-up email</h3>
                <p className="mt-2 text-sm text-text-muted">
                    No AI follow-up draft yet for this interaction. Drafts
                    appear after Linda finishes processing the call.
                </p>
                <button
                    type="button"
                    onClick={() => draft.refetch()}
                    className="mt-3 rounded-md border border-border px-3 py-1.5 text-xs hover:bg-bg-secondary"
                >
                    Try again
                </button>
            </section>
        );
    }

    const validRecipients = recipients
        .map((r) => r.trim())
        .filter((r) => r.length > 0);
    const canSend =
        hasAnyProvider &&
        subject.trim().length > 0 &&
        body.trim().length > 0 &&
        validRecipients.length > 0 &&
        !send.isPending;

    function updateRecipient(idx: number, value: string) {
        setRecipients((cur) => cur.map((r, i) => (i === idx ? value : r)));
    }

    function addRecipient() {
        setRecipients((cur) => [...cur, ""]);
    }

    function removeRecipient(idx: number) {
        setRecipients((cur) =>
            cur.length > 1 ? cur.filter((_, i) => i !== idx) : cur,
        );
    }

    async function handleSend() {
        const to = validRecipients[0];
        if (!to) return;
        // Backend's current EmailSendIn accepts only single `to` + `cc`.
        // Extra recipients are appended into CC so they still receive the
        // mail; surfacing this in the input help text would require a
        // schema change to accept arrays.
        const extraTo = validRecipients.slice(1);
        const ccCombined = [
            ...extraTo,
            ...(cc ? cc.split(",").map((s) => s.trim()).filter(Boolean) : []),
            ...(bcc ? bcc.split(",").map((s) => s.trim()).filter(Boolean) : []),
        ].join(", ");
        try {
            const result = await send.mutateAsync({
                subject: subject.trim(),
                body,
                to,
                cc: ccCombined || undefined,
                provider: provider === "auto" ? undefined : provider,
                attachments:
                    attachments.length > 0
                        ? attachments.map((a) => ({
                              kind: a.kind,
                              id: a.id,
                              title: a.title,
                          }))
                        : undefined,
            });
            setLastSent(result);
            setAttachments([]);
        } catch {
            // surfaced inline below
        }
    }

    function handleRegenerate() {
        setHydrated(false);
        draft.refetch();
    }

    return (
        <section
            id="follow-up"
            ref={sectionRef}
            className="rounded-lg border border-border bg-bg-card p-5"
        >
            <div className="flex items-start justify-between gap-4">
                <div>
                    <h3 className="text-sm font-semibold">Follow-up email</h3>
                    <p className="mt-1 text-xs text-text-muted">
                        Linda drafted this from the call. Edit anything before
                        sending.
                    </p>
                </div>
                <div className="flex shrink-0 gap-2">
                    <button
                        type="button"
                        onClick={handleRegenerate}
                        disabled={draft.isFetching}
                        className="rounded-md border border-border px-3 py-1.5 text-xs hover:bg-bg-secondary disabled:opacity-50"
                    >
                        {draft.isFetching ? "Regenerating…" : "Regenerate"}
                    </button>
                    <button
                        type="button"
                        onClick={() => setCollapsed(true)}
                        className="rounded-md border border-border px-3 py-1.5 text-xs hover:bg-bg-secondary"
                    >
                        Discard draft
                    </button>
                </div>
            </div>

            {!hasAnyProvider ? (
                <div className="mt-4 rounded-md border border-accent-amber/40 bg-accent-amber/10 p-3 text-xs text-text-muted">
                    No Gmail or Outlook integration is connected for this
                    tenant.{" "}
                    <Link
                        href="/settings#integrations"
                        className="text-primary hover:underline"
                    >
                        Connect one in Settings
                    </Link>{" "}
                    to send this follow-up.
                </div>
            ) : null}

            <div className="mt-4 space-y-3">
                <label className="block text-sm">
                    <span className="text-xs uppercase tracking-wide text-text-subtle">
                        Subject
                    </span>
                    <input
                        type="text"
                        value={subject}
                        onChange={(e) => setSubject(e.target.value)}
                        className="mt-1 w-full rounded-md border border-border bg-bg-secondary px-3 py-2 text-sm outline-none focus:border-primary"
                    />
                </label>

                <div className="block text-sm">
                    <span className="text-xs uppercase tracking-wide text-text-subtle">
                        To
                    </span>
                    <div className="mt-1 space-y-2">
                        {recipients.map((r, idx) => (
                            <div key={idx} className="flex gap-2">
                                <input
                                    type="email"
                                    value={r}
                                    onChange={(e) =>
                                        updateRecipient(idx, e.target.value)
                                    }
                                    placeholder="recipient@example.com"
                                    className="flex-1 rounded-md border border-border bg-bg-secondary px-3 py-2 text-sm outline-none focus:border-primary"
                                />
                                {recipients.length > 1 ? (
                                    <button
                                        type="button"
                                        onClick={() => removeRecipient(idx)}
                                        className="rounded-md border border-border px-2 py-1 text-xs text-text-muted hover:bg-bg-secondary"
                                    >
                                        Remove
                                    </button>
                                ) : null}
                            </div>
                        ))}
                        <button
                            type="button"
                            onClick={addRecipient}
                            className="text-xs text-primary hover:underline"
                        >
                            + Add recipient
                        </button>
                    </div>
                </div>

                {showCcBcc ? (
                    <>
                        <label className="block text-sm">
                            <span className="text-xs uppercase tracking-wide text-text-subtle">
                                CC
                            </span>
                            <input
                                type="text"
                                value={cc}
                                onChange={(e) => setCc(e.target.value)}
                                placeholder="comma-separated"
                                className="mt-1 w-full rounded-md border border-border bg-bg-secondary px-3 py-2 text-sm outline-none focus:border-primary"
                            />
                        </label>
                        <label className="block text-sm">
                            <span className="text-xs uppercase tracking-wide text-text-subtle">
                                BCC
                            </span>
                            <input
                                type="text"
                                value={bcc}
                                onChange={(e) => setBcc(e.target.value)}
                                placeholder="comma-separated"
                                className="mt-1 w-full rounded-md border border-border bg-bg-secondary px-3 py-2 text-sm outline-none focus:border-primary"
                            />
                        </label>
                    </>
                ) : (
                    <button
                        type="button"
                        onClick={() => setShowCcBcc(true)}
                        className="text-xs text-primary hover:underline"
                    >
                        + Add CC / BCC
                    </button>
                )}

                <label className="block text-sm">
                    <span className="text-xs uppercase tracking-wide text-text-subtle">
                        Body
                    </span>
                    <textarea
                        value={body}
                        onChange={(e) => setBody(e.target.value)}
                        rows={10}
                        className="mt-1 w-full rounded-md border border-border bg-bg-secondary px-3 py-2 text-sm outline-none focus:border-primary"
                    />
                </label>

                <div className="flex flex-wrap items-center gap-2 pt-1">
                    <button
                        type="button"
                        onClick={() => setShowPicker(true)}
                        className="rounded border border-border bg-bg-secondary px-3 py-1.5 text-xs hover:bg-card-hover"
                    >
                        📎 Attach from KB
                    </button>
                    {attachments.map((a) => (
                        <span
                            key={a.id}
                            className="inline-flex items-center gap-1 rounded-full border border-border bg-primary-soft px-2 py-0.5 text-xs text-primary"
                            title={a.title}
                        >
                            <span className="max-w-[160px] truncate">
                                {a.title}
                            </span>
                            <button
                                type="button"
                                onClick={() =>
                                    setAttachments((prev) =>
                                        prev.filter((p) => p.id !== a.id),
                                    )
                                }
                                aria-label={`Remove ${a.title}`}
                                className="text-text-muted hover:text-text"
                            >
                                ×
                            </button>
                        </span>
                    ))}
                </div>

                <KBFilePickerModal
                    open={showPicker}
                    onClose={() => setShowPicker(false)}
                    onConfirm={(picked) => {
                        setAttachments(picked);
                        setShowPicker(false);
                    }}
                    initialSelection={attachments}
                />

                <div className="flex flex-wrap items-center justify-between gap-3 pt-1">
                    <label className="flex items-center gap-2 text-xs text-text-muted">
                        Send via
                        <select
                            value={provider}
                            onChange={(e) =>
                                setProvider(
                                    e.target.value as ProviderChoice,
                                )
                            }
                            className="rounded-md border border-border bg-bg-secondary px-2 py-1 text-xs"
                        >
                            <option value="auto">Auto</option>
                            <option value="google" disabled={!hasGoogle}>
                                Gmail{hasGoogle ? " (connected)" : " (not connected)"}
                            </option>
                            <option
                                value="microsoft"
                                disabled={!hasMicrosoft}
                            >
                                Outlook
                                {hasMicrosoft
                                    ? " (connected)"
                                    : " (not connected)"}
                            </option>
                        </select>
                    </label>
                    <button
                        type="button"
                        onClick={handleSend}
                        disabled={!canSend}
                        className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-white hover:bg-primary-hover disabled:opacity-50"
                    >
                        {send.isPending ? "Sending…" : "Send"}
                    </button>
                </div>

                {send.isError ? (
                    <p className="text-xs text-accent-rose">
                        Couldn&apos;t send:{" "}
                        {send.error instanceof Error
                            ? send.error.message
                            : "unknown error"}
                    </p>
                ) : null}
            </div>

            {draft.data?.recent_sends &&
            draft.data.recent_sends.length > 0 ? (
                <div className="mt-5 border-t border-border pt-3">
                    <h4 className="text-xs uppercase tracking-wide text-text-subtle">
                        Recent sends
                    </h4>
                    <ul className="mt-2 space-y-1 text-xs text-text-muted">
                        {draft.data.recent_sends.slice(0, 5).map((s) => (
                            <li
                                key={s.id}
                                className="flex flex-wrap items-center gap-2"
                            >
                                <span
                                    className={
                                        s.status === "sent"
                                            ? "text-accent-emerald"
                                            : s.status === "failed"
                                              ? "text-accent-rose"
                                              : "text-accent-amber"
                                    }
                                >
                                    {s.status}
                                </span>
                                <span>→ {s.to_address}</span>
                                <span className="text-text-subtle">
                                    {s.sent_at
                                        ? new Date(s.sent_at).toLocaleString()
                                        : new Date(
                                              s.created_at,
                                          ).toLocaleString()}
                                </span>
                            </li>
                        ))}
                    </ul>
                </div>
            ) : null}
        </section>
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
            const all = await api.get<ActionItem[]>(
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
                <div className="mt-3 space-y-2">
                    {items.data.map((it) => (
                        <ActionItemCard key={it.id} item={it} />
                    ))}
                </div>
            ) : (
                <p className="mt-3 text-sm text-text-subtle">
                    No action items pulled from this interaction yet.
                </p>
            )}
        </section>
    );
}

/* ── Coaching tab ───────────────────────────────────────────────── */

function CoachingTab({
    insights,
}: {
    insights: import("@/lib/interactions").InteractionInsights | undefined;
}) {
    const coaching = insights?.coaching;
    const evidence = insights?.evidence;
    const rubric = insights?.rubric;
    const rapport = insights?.rapport;
    const snippets = insights?.notable_snippets ?? [];

    const wentWell = coaching?.what_went_well ?? [];
    const improvements = coaching?.improvements ?? [];

    return (
        <div className="space-y-6">
            {/* Rapport gauge. LSM (transcript) + vocal accommodation
                (audio-prosody). Either half is enough to render; the
                composite ``rapport.overall`` blends both when both are
                present. Hidden only when neither half computed. */}
            {(typeof rapport?.lsm_overall === "number" ||
                typeof rapport?.vocal_accommodation?.overall === "number") && (
                <RapportCard rapport={rapport!} />
            )}
            {/*
                Reinforcement parity: "What went well" sits left of
                "Try next time" so the rep sees the positive read first.
                We deliberately render both columns even when one side
                is empty. an empty "well done" column is a stronger
                signal than no column at all.
            */}
            <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
                <section className="rounded-lg border border-border bg-bg-card p-5">
                    <h3 className="text-sm font-semibold text-accent-emerald">
                        What went well
                    </h3>
                    {wentWell.length > 0 ? (
                        <ul className="mt-3 space-y-2 text-sm text-text">
                            {wentWell.map((item, idx) => (
                                <li key={idx} className="flex gap-2">
                                    <span
                                        aria-hidden
                                        className="mt-0.5 inline-block h-2 w-2 shrink-0 rounded-full bg-accent-emerald"
                                    />
                                    <span>{item}</span>
                                </li>
                            ))}
                        </ul>
                    ) : (
                        <p className="mt-3 text-sm text-text-subtle">
                            No specific positives surfaced for this call.
                        </p>
                    )}
                </section>
                <section className="rounded-lg border border-border bg-bg-card p-5">
                    <h3 className="text-sm font-semibold text-accent-amber">
                        Try next time
                    </h3>
                    {improvements.length > 0 ? (
                        <ul className="mt-3 space-y-2 text-sm text-text">
                            {improvements.map((item, idx) => (
                                <li key={idx} className="flex gap-2">
                                    <span
                                        aria-hidden
                                        className="mt-0.5 inline-block h-2 w-2 shrink-0 rounded-full bg-accent-amber"
                                    />
                                    <span>{item}</span>
                                </li>
                            ))}
                        </ul>
                    ) : (
                        <p className="mt-3 text-sm text-text-subtle">
                            Nothing flagged for improvement on this call.
                        </p>
                    )}
                </section>
            </div>

            {/* Evidence + rubric. Phase 3 surfacing. */}
            {(evidence || rubric) && (
                <section className="rounded-lg border border-border bg-bg-card p-5">
                    <h3 className="text-sm font-semibold">
                        Evidence-derived rubric
                    </h3>
                    <p className="mt-1 text-xs text-text-subtle">
                        Each score is derived from concrete moments in the
                        call. Hover any cell to see how it&apos;s computed.
                    </p>
                    <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
                        <RubricCell
                            label="Discovery"
                            value={rubric?.discovery_quality}
                            sub={
                                evidence?.discovery_questions != null
                                    ? `${evidence.discovery_questions} questions`
                                    : undefined
                            }
                        />
                        <RubricCell
                            label="Commitments"
                            value={rubric?.commitment_strength}
                            sub={
                                evidence?.commitment_count != null
                                    ? `${evidence.commitment_count} secured`
                                    : undefined
                            }
                        />
                        <RubricCell
                            label="Objection handling"
                            value={rubric?.objection_resolution_rate}
                            sub={
                                evidence?.objection_count != null
                                    ? `${
                                          (evidence.objection_count || 0) -
                                          (evidence.unresolved_objection_count ||
                                              0)
                                      } / ${evidence.objection_count} resolved`
                                    : undefined
                            }
                        />
                        <RubricCell
                            label="Win likelihood"
                            value={rubric?.win_likelihood}
                            sub={
                                evidence?.competitor_mention_count != null
                                    ? `${evidence.competitor_mention_count} competitor mentions`
                                    : undefined
                            }
                        />
                    </div>
                </section>
            )}

            {/* Notable snippets. LLM has been emitting these since
                Phase 5b but no UI surface rendered them. They live here
                because they double as coaching artifacts (manager picks
                a great moment to share with the team). */}
            {snippets.length > 0 && <NotableSnippetsCard snippets={snippets} />}
        </div>
    );
}

function RapportCard({
    rapport,
}: {
    rapport: NonNullable<
        import("@/lib/interactions").InteractionInsights["rapport"]
    >;
}) {
    // Composite uses ``overall`` when both halves landed; falls back
    // to whichever half is present. Pinned by ``attach_vocal_accommodation``
    // on the backend.
    const overall =
        rapport.overall ??
        rapport.lsm_overall ??
        rapport.vocal_accommodation?.overall ??
        0;
    const pct = Math.round(overall * 100);
    // Bands tuned around the Pennebaker reference: typical free-flow
    // dialogue lands ~0.65–0.80; below 0.5 reads as misalignment.
    const band =
        overall >= 0.75
            ? { label: "Strong mirroring", tone: "emerald" as const }
            : overall >= 0.6
              ? { label: "Aligned", tone: "amber" as const }
              : { label: "Drifting", tone: "rose" as const };
    const tone = {
        emerald:
            "border-accent-emerald/40 bg-accent-emerald/10 text-accent-emerald",
        amber: "border-accent-amber/40 bg-accent-amber/10 text-accent-amber",
        rose: "border-accent-rose/40 bg-accent-rose/10 text-accent-rose",
    }[band.tone];
    const hasVocal =
        typeof rapport.vocal_accommodation?.overall === "number";
    const hasLsm = typeof rapport.lsm_overall === "number";
    return (
        <section className="rounded-lg border border-border bg-bg-card p-5">
            <header className="flex flex-wrap items-baseline justify-between gap-3">
                <div>
                    <h3 className="text-sm font-semibold">Rapport</h3>
                    <p className="mt-1 text-xs text-text-subtle">
                        Composite of Linguistic Style Matching (function-word
                        mirroring) and vocal accommodation (prosodic
                        convergence). Both halves are deterministic -
                        no model gut-feel.
                    </p>
                </div>
                <span
                    className={`rounded-full border px-3 py-1 text-xs ${tone}`}
                >
                    {pct}% · {band.label}
                </span>
            </header>

            {/* Per-half summary chips so the rep can see which side of
                the composite is driving the overall number. */}
            <div className="mt-3 flex flex-wrap gap-2 text-xs">
                {hasLsm && (
                    <span className="rounded-md border border-border-light bg-bg-secondary px-2 py-1">
                        LSM (text):{" "}
                        <span className="font-semibold">
                            {Math.round((rapport.lsm_overall ?? 0) * 100)}%
                        </span>
                    </span>
                )}
                {hasVocal && (
                    <span className="rounded-md border border-border-light bg-bg-secondary px-2 py-1">
                        Vocal accommodation:{" "}
                        <span className="font-semibold">
                            {Math.round(
                                (rapport.vocal_accommodation?.overall ?? 0) *
                                    100,
                            )}
                            %
                        </span>
                    </span>
                )}
            </div>

            {rapport.lsm_by_category && (
                <div className="mt-4 grid grid-cols-2 gap-2 sm:grid-cols-4">
                    {Object.entries(rapport.lsm_by_category)
                        .sort((a, b) => b[1] - a[1])
                        .map(([cat, val]) => (
                            <div
                                key={cat}
                                className="rounded-md border border-border-light bg-bg-secondary px-3 py-2"
                            >
                                <div className="text-[11px] uppercase tracking-wide text-text-subtle">
                                    {cat.replace(/_/g, " ")}
                                </div>
                                <div className="mt-1 text-sm font-semibold text-text">
                                    {Math.round(val * 100)}%
                                </div>
                            </div>
                        ))}
                </div>
            )}
        </section>
    );
}

function NotableSnippetsCard({
    snippets,
}: {
    snippets: NonNullable<
        import("@/lib/interactions").InteractionInsights["notable_snippets"]
    >;
}) {
    return (
        <section className="rounded-lg border border-border bg-bg-card p-5">
            <header>
                <h3 className="text-sm font-semibold">Notable moments</h3>
                <p className="mt-1 text-xs text-text-subtle">
                    Specific moments worth re-listening to and sharing with
                    the team.
                </p>
            </header>
            <ul className="mt-3 space-y-2">
                {snippets.map((s, idx) => {
                    const tone =
                        s.quality === "positive"
                            ? "border-accent-emerald/40"
                            : s.quality === "negative"
                              ? "border-accent-rose/40"
                              : "border-border-light";
                    return (
                        <li
                            key={idx}
                            className={`rounded-md border ${tone} bg-bg-secondary px-3 py-2`}
                        >
                            <div className="flex flex-wrap items-baseline justify-between gap-2">
                                <span className="text-sm font-medium text-text">
                                    {s.title || s.type || "Notable moment"}
                                </span>
                                {s.start_time && (
                                    <span className="text-[11px] text-text-subtle">
                                        {s.start_time}
                                        {s.end_time ? ` to ${s.end_time}` : ""}
                                    </span>
                                )}
                            </div>
                            {s.description && (
                                <p className="mt-1 text-xs text-text-muted">
                                    {s.description}
                                </p>
                            )}
                            {s.tags && s.tags.length > 0 && (
                                <div className="mt-1 flex flex-wrap gap-1">
                                    {s.tags.map((t, i) => (
                                        <span
                                            key={i}
                                            className="rounded bg-bg-card px-1.5 py-0.5 text-[10px] text-text-subtle"
                                        >
                                            #{t}
                                        </span>
                                    ))}
                                </div>
                            )}
                        </li>
                    );
                })}
            </ul>
        </section>
    );
}

function RubricCell({
    label,
    value,
    sub,
}: {
    label: string;
    value: number | undefined;
    sub?: string;
}) {
    const pct =
        typeof value === "number" ? Math.round(value * 100) : undefined;
    return (
        <div className="rounded-md border border-border bg-bg-secondary px-3 py-2">
            <div className="text-xs uppercase tracking-wide text-text-subtle">
                {label}
            </div>
            <div className="mt-1 text-lg font-semibold text-text">
                {pct != null ? `${pct}%` : "-"}
            </div>
            {sub ? (
                <div className="text-[11px] text-text-muted">{sub}</div>
            ) : null}
        </div>
    );
}

/* ── Compliance tab ─────────────────────────────────────────────── */

function ComplianceTab({
    insights,
}: {
    insights: import("@/lib/interactions").InteractionInsights | undefined;
}) {
    const coaching = insights?.coaching;
    const adherenceBand = coaching?.script_adherence_band as
        | string
        | undefined;
    const adherenceScore = coaching?.script_adherence_score;
    const gaps = coaching?.compliance_gaps ?? [];
    const coverage = insights?.methodology_coverage;

    const bandTone =
        adherenceBand === "high"
            ? "bg-accent-emerald/10 text-accent-emerald border-accent-emerald/40"
            : adherenceBand === "medium"
              ? "bg-accent-amber/10 text-accent-amber border-accent-amber/40"
              : adherenceBand === "low" || adherenceBand === "failing"
                ? "bg-accent-rose/10 text-accent-rose border-accent-rose/40"
                : "bg-bg-secondary text-text-muted border-border";

    return (
        <div className="space-y-6">
            <section className="rounded-lg border border-border bg-bg-card p-5">
                <div className="flex flex-wrap items-baseline justify-between gap-3">
                    <div>
                        <h3 className="text-sm font-semibold">
                            Script adherence
                        </h3>
                        <p className="mt-1 text-xs text-text-subtle">
                            Bucketed call by call; mapped to a numeric score
                            for trend dashboards.
                        </p>
                    </div>
                    <span
                        className={`rounded-full border px-3 py-1 text-xs capitalize ${bandTone}`}
                    >
                        {adherenceBand ?? "not scored"}
                        {adherenceScore != null
                            ? ` · ${adherenceScore.toFixed(0)} / 100`
                            : ""}
                    </span>
                </div>
                {gaps.length > 0 ? (
                    <div className="mt-4">
                        <h4 className="text-xs uppercase tracking-wide text-text-subtle">
                            Compliance gaps
                        </h4>
                        <ul className="mt-2 space-y-1 text-sm text-text">
                            {gaps.map((g, idx) => (
                                <li
                                    key={idx}
                                    className="flex gap-2 rounded-md border border-accent-rose/30 bg-accent-rose/5 px-3 py-2"
                                >
                                    <span aria-hidden>⚠</span>
                                    <span>{g}</span>
                                </li>
                            ))}
                        </ul>
                    </div>
                ) : (
                    <p className="mt-4 text-sm text-text-subtle">
                        No compliance gaps flagged.
                    </p>
                )}
            </section>

            <MethodologyScorecard coverage={coverage} />
        </div>
    );
}
