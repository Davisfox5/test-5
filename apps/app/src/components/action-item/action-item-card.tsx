"use client";

import { useState } from "react";
import {
    type ActionItem,
    useUpdateActionItem,
    useReturnActionItem,
    useScheduleMeeting,
    useActionItemFeedback,
    useTenantUsers,
} from "@/lib/action-items";
import { ActionItemComments } from "./action-item-comments";
import { useContextDrawer } from "../context-drawer/context-drawer";

/**
 * Action Item Card — compact + expanded.
 *
 * Compact (default): one row showing title, channel icon, due-date pill,
 * priority dot, done checkbox, expand chevron.
 * Expanded: full detail (description, channel reasoning, participants,
 * prep artifacts, implicit signal, draft) plus action buttons (lifecycle
 * + channel-primary + secondary cluster), comments thread, inline edit.
 *
 * State changes are optimistic via React Query so the rep doesn't see a
 * spinner on common interactions.
 */
export function ActionItemCard({ item }: { item: ActionItem }) {
    const [expanded, setExpanded] = useState(false);
    return (
        <article
            className="rounded-md border border-border bg-card transition-colors hover:bg-card-hover"
            data-action-item={item.id}
        >
            <CompactRow
                item={item}
                expanded={expanded}
                onToggle={() => setExpanded((v) => !v)}
            />
            {expanded && <ExpandedPanel item={item} />}
        </article>
    );
}

// ── Compact row ─────────────────────────────────────────────────────────

function CompactRow({
    item,
    expanded,
    onToggle,
}: {
    item: ActionItem;
    expanded: boolean;
    onToggle: () => void;
}) {
    const update = useUpdateActionItem();

    const isDone = item.status === "done" || item.status === "completed";
    const channelIcon = channelIconFor(item.recommended_channel);
    const isImplicit = Boolean(item.implicit_signal);

    return (
        <div className="flex items-center gap-3 px-3 py-2">
            <input
                type="checkbox"
                checked={isDone}
                aria-label={isDone ? "Mark not done" : "Mark done"}
                onChange={(e) =>
                    update.mutate({
                        id: item.id,
                        patch: { status: e.target.checked ? "done" : "open" },
                    })
                }
                className="h-4 w-4 cursor-pointer rounded border border-border-strong accent-primary"
            />
            <button
                type="button"
                onClick={onToggle}
                className="flex flex-1 items-center gap-2 text-left"
                aria-expanded={expanded}
            >
                <span
                    className={`flex-1 truncate text-sm ${
                        isDone ? "text-text-muted line-through" : "text-text"
                    }`}
                >
                    {item.title}
                </span>
                {item.recommended_channel && (
                    <span
                        title={`Channel: ${item.recommended_channel}`}
                        className="text-base"
                        aria-hidden
                    >
                        {channelIcon}
                    </span>
                )}
                {item.due_date && <DueDatePill due={item.due_date} />}
                <PriorityDot priority={item.priority} />
                {isImplicit && (
                    <span
                        title="Includes a signal the rep may not have noticed"
                        className="rounded bg-accent-amber/30 px-1 text-xs font-medium text-text"
                        aria-label="Implicit signal"
                    >
                        signal
                    </span>
                )}
                <span
                    aria-hidden
                    className={`text-text-subtle transition-transform ${
                        expanded ? "rotate-90" : ""
                    }`}
                >
                    ›
                </span>
            </button>
        </div>
    );
}

// ── Expanded panel ──────────────────────────────────────────────────────

function ExpandedPanel({ item }: { item: ActionItem }) {
    return (
        <div className="space-y-4 border-t border-border-light px-3 py-3">
            {item.description && (
                <p className="text-sm text-text">{item.description}</p>
            )}
            {item.implicit_signal && (
                <div className="rounded border border-accent-amber/40 bg-accent-amber/10 p-2 text-xs text-text">
                    <span className="font-medium">What the rep may have missed:</span>{" "}
                    {item.implicit_signal}
                </div>
            )}

            <ChannelBlock item={item} />
            <ParticipantsBlock item={item} />
            <PrepArtifactsBlock item={item} />
            <SuggestedAttachmentsBlock item={item} />
            <ActionButtonRow item={item} />

            <CommentsBlock item={item} />
        </div>
    );
}

function ChannelBlock({ item }: { item: ActionItem }) {
    if (!item.recommended_channel && !item.channel_reasoning) return null;
    return (
        <section>
            <h4 className="mb-1 text-xs font-semibold uppercase tracking-wide text-text-muted">
                Recommended channel
            </h4>
            <p className="text-sm text-text">
                <span className="mr-1" aria-hidden>
                    {channelIconFor(item.recommended_channel)}
                </span>
                <span className="font-medium capitalize">
                    {item.recommended_channel?.replace("_", " ") || "—"}
                </span>
                {item.channel_reasoning && (
                    <span className="text-text-muted"> — {item.channel_reasoning}</span>
                )}
            </p>
            {item.recommended_channel === "phone_call" &&
                item.call_script &&
                item.call_script.length > 0 && (
                    <ul className="mt-2 list-disc space-y-1 pl-5 text-sm text-text">
                        {item.call_script.map((b, i) => (
                            <li key={i}>{b}</li>
                        ))}
                    </ul>
                )}
            {item.email_draft &&
                (item.recommended_channel === "email" ||
                    item.recommended_channel === "document_send") && (
                    <EmailDraftPreview draft={item.email_draft} />
                )}
        </section>
    );
}

function EmailDraftPreview({ draft }: { draft: Record<string, unknown> }) {
    const subject = String(draft.subject ?? "");
    const body = String(draft.body ?? "");
    return (
        <div className="mt-2 rounded border border-border-light bg-bg-secondary p-2 text-sm">
            <div className="mb-1 text-xs font-medium text-text-muted">Email draft</div>
            {subject && <div className="font-semibold text-text">{subject}</div>}
            {body && (
                <pre className="mt-1 whitespace-pre-wrap font-sans text-text">
                    {body}
                </pre>
            )}
        </div>
    );
}

function ParticipantsBlock({ item }: { item: ActionItem }) {
    if (!item.participants || item.participants.length === 0) return null;
    return (
        <section>
            <h4 className="mb-1 text-xs font-semibold uppercase tracking-wide text-text-muted">
                Participants
            </h4>
            <ul className="flex flex-wrap gap-1">
                {item.participants.map((p, i) => (
                    <li
                        key={i}
                        className="rounded-full border border-border bg-bg-secondary px-2 py-0.5 text-xs text-text"
                        title={p.email ?? "Email not resolved"}
                    >
                        <span className="font-medium">{p.name}</span>
                        {p.role && (
                            <span className="text-text-muted"> · {p.role}</span>
                        )}
                        {p.side && (
                            <span className="text-text-subtle"> · {p.side}</span>
                        )}
                    </li>
                ))}
            </ul>
        </section>
    );
}

function PrepArtifactsBlock({ item }: { item: ActionItem }) {
    if (!item.prep_artifacts || item.prep_artifacts.length === 0) return null;
    return (
        <section>
            <h4 className="mb-1 text-xs font-semibold uppercase tracking-wide text-text-muted">
                Prep
            </h4>
            <ul className="list-disc space-y-1 pl-5 text-sm text-text">
                {item.prep_artifacts.map((a, i) => (
                    <li key={i}>{a}</li>
                ))}
            </ul>
        </section>
    );
}

function SuggestedAttachmentsBlock({ item }: { item: ActionItem }) {
    if (!item.suggested_attachments || item.suggested_attachments.length === 0)
        return null;
    return (
        <section>
            <h4 className="mb-1 text-xs font-semibold uppercase tracking-wide text-text-muted">
                Suggested attachments
            </h4>
            <ul className="space-y-1 text-sm text-text">
                {item.suggested_attachments.map((a, i) => (
                    <li key={i}>
                        <span className="font-medium">{a.title}</span>
                        {a.reason && (
                            <span className="text-text-muted"> — {a.reason}</span>
                        )}
                    </li>
                ))}
            </ul>
        </section>
    );
}

// ── Action buttons ──────────────────────────────────────────────────────

function ActionButtonRow({ item }: { item: ActionItem }) {
    const update = useUpdateActionItem();
    const returnItem = useReturnActionItem();
    const schedule = useScheduleMeeting();
    const feedback = useActionItemFeedback();
    const drawer = useContextDrawer();
    const { data: users = [] } = useTenantUsers();

    const [showSnooze, setShowSnooze] = useState(false);
    const [showDismiss, setShowDismiss] = useState(false);
    const [showReturn, setShowReturn] = useState(false);
    const [showReassign, setShowReassign] = useState(false);
    const [scheduleNote, setScheduleNote] = useState<string | null>(null);

    const isMeetingChannel =
        item.recommended_channel === "meeting" ||
        item.recommended_channel === "phone_call";
    const isEmailChannel =
        item.recommended_channel === "email" ||
        item.recommended_channel === "document_send";

    return (
        <section className="space-y-2">
            <div className="flex flex-wrap gap-2">
                {isMeetingChannel && (
                    <button
                        type="button"
                        onClick={async () => {
                            setScheduleNote(null);
                            const result = await schedule.mutateAsync({
                                id: item.id,
                                payload: {},
                            });
                            if (result.success) {
                                setScheduleNote(
                                    result.join_url
                                        ? `Created. Join: ${result.join_url}`
                                        : (result.note ??
                                          "Created (calendar invite ready)."),
                                );
                            } else {
                                setScheduleNote(
                                    `Failed: ${result.error ?? "unknown error"}`,
                                );
                            }
                        }}
                        disabled={schedule.isPending}
                        className="rounded bg-primary px-3 py-1.5 text-sm font-medium text-white hover:bg-primary-hover disabled:opacity-50"
                    >
                        {item.recommended_channel === "phone_call"
                            ? "Schedule call"
                            : "Schedule meeting"}
                    </button>
                )}
                {isEmailChannel && (
                    <a
                        href={`/interactions/${item.interaction_id}#follow-up`}
                        className="rounded bg-primary px-3 py-1.5 text-sm font-medium text-white hover:bg-primary-hover"
                    >
                        {item.recommended_channel === "document_send"
                            ? "Send document"
                            : "Send email"}
                    </a>
                )}
                <button
                    type="button"
                    onClick={() => setShowSnooze((v) => !v)}
                    className="rounded border border-border bg-card px-3 py-1.5 text-sm hover:bg-card-hover"
                >
                    Snooze
                </button>
                <button
                    type="button"
                    onClick={() => setShowDismiss((v) => !v)}
                    className="rounded border border-border bg-card px-3 py-1.5 text-sm hover:bg-card-hover"
                >
                    Dismiss
                </button>
                <button
                    type="button"
                    onClick={() => setShowReassign((v) => !v)}
                    className="rounded border border-border bg-card px-3 py-1.5 text-sm hover:bg-card-hover"
                >
                    Reassign
                </button>
                {item.assigned_to && (
                    <button
                        type="button"
                        onClick={() => setShowReturn((v) => !v)}
                        className="rounded border border-border bg-card px-3 py-1.5 text-sm hover:bg-card-hover"
                    >
                        Return
                    </button>
                )}
                <button
                    type="button"
                    onClick={() =>
                        drawer.open({
                            title: "Source moment",
                            body: (
                                <p className="text-sm text-text-muted">
                                    Source linkage from action item to transcript.
                                    Wire-up of timestamp jumping ships with the
                                    transcript-tab redesign.
                                </p>
                            ),
                        })
                    }
                    className="rounded border border-border bg-card px-3 py-1.5 text-sm hover:bg-card-hover"
                >
                    Jump to source
                </button>
                <FeedbackButtons
                    score={item.feedback_score ?? 0}
                    onClick={(helpful) =>
                        feedback.mutate({ id: item.id, helpful })
                    }
                />
            </div>

            {scheduleNote && (
                <div className="rounded border border-border-light bg-bg-secondary px-2 py-1 text-xs text-text">
                    {scheduleNote}
                </div>
            )}

            {showSnooze && (
                <SnoozeForm
                    onCancel={() => setShowSnooze(false)}
                    onSubmit={(iso) => {
                        update.mutate({
                            id: item.id,
                            patch: { snoozed_until: iso },
                        });
                        setShowSnooze(false);
                    }}
                />
            )}
            {showDismiss && (
                <DismissForm
                    onCancel={() => setShowDismiss(false)}
                    onSubmit={(reason) => {
                        update.mutate({
                            id: item.id,
                            patch: { status: "dismissed", dismiss_reason: reason },
                        });
                        setShowDismiss(false);
                    }}
                />
            )}
            {showReassign && (
                <ReassignForm
                    users={users}
                    currentAssignee={item.assigned_to}
                    onCancel={() => setShowReassign(false)}
                    onSubmit={(userId) => {
                        update.mutate({
                            id: item.id,
                            patch: { assigned_to: userId },
                        });
                        setShowReassign(false);
                    }}
                />
            )}
            {showReturn && (
                <ReturnForm
                    onCancel={() => setShowReturn(false)}
                    onSubmit={(reason) => {
                        returnItem.mutate({ id: item.id, reason });
                        setShowReturn(false);
                    }}
                />
            )}
        </section>
    );
}

// ── Comments thread ─────────────────────────────────────────────────────

function CommentsBlock({ item }: { item: ActionItem }) {
    return (
        <section>
            <h4 className="mb-2 text-xs font-semibold uppercase tracking-wide text-text-muted">
                Discussion
            </h4>
            <ActionItemComments actionItemId={item.id} />
        </section>
    );
}

// ── Form pieces (inline, not modals) ────────────────────────────────────

function SnoozeForm({
    onCancel,
    onSubmit,
}: {
    onCancel: () => void;
    onSubmit: (iso: string) => void;
}) {
    const [iso, setIso] = useState(() => {
        const d = new Date();
        d.setDate(d.getDate() + 1);
        return d.toISOString().slice(0, 16);
    });
    const presets = [
        { label: "1 day", days: 1 },
        { label: "3 days", days: 3 },
        { label: "1 week", days: 7 },
        { label: "2 weeks", days: 14 },
    ];
    return (
        <div className="rounded border border-border-light bg-bg-secondary p-2 text-sm">
            <div className="mb-2 flex flex-wrap gap-1">
                {presets.map((p) => (
                    <button
                        key={p.label}
                        type="button"
                        onClick={() => {
                            const d = new Date();
                            d.setDate(d.getDate() + p.days);
                            onSubmit(d.toISOString());
                        }}
                        className="rounded border border-border bg-card px-2 py-0.5 text-xs hover:bg-card-hover"
                    >
                        {p.label}
                    </button>
                ))}
            </div>
            <label className="block text-xs text-text-muted">Or pick a date/time</label>
            <input
                type="datetime-local"
                value={iso}
                onChange={(e) => setIso(e.target.value)}
                className="mt-1 w-full rounded border border-border bg-card px-2 py-1 text-sm text-text"
            />
            <div className="mt-2 flex gap-2">
                <button
                    type="button"
                    onClick={() => onSubmit(new Date(iso).toISOString())}
                    className="rounded bg-primary px-2 py-1 text-xs font-medium text-white hover:bg-primary-hover"
                >
                    Snooze
                </button>
                <button
                    type="button"
                    onClick={onCancel}
                    className="rounded border border-border bg-card px-2 py-1 text-xs hover:bg-card-hover"
                >
                    Cancel
                </button>
            </div>
        </div>
    );
}

function DismissForm({
    onCancel,
    onSubmit,
}: {
    onCancel: () => void;
    onSubmit: (reason: string) => void;
}) {
    const [reason, setReason] = useState("");
    const presets = [
        "Not relevant",
        "Already handled",
        "Wrong customer",
        "Low priority",
        "Wrong recommendation",
    ];
    return (
        <div className="rounded border border-border-light bg-bg-secondary p-2 text-sm">
            <div className="mb-2 flex flex-wrap gap-1">
                {presets.map((p) => (
                    <button
                        key={p}
                        type="button"
                        onClick={() => onSubmit(p)}
                        className="rounded border border-border bg-card px-2 py-0.5 text-xs hover:bg-card-hover"
                    >
                        {p}
                    </button>
                ))}
            </div>
            <textarea
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                placeholder="Or write a reason…"
                rows={2}
                className="w-full rounded border border-border bg-card px-2 py-1 text-sm text-text"
            />
            <div className="mt-2 flex gap-2">
                <button
                    type="button"
                    disabled={!reason.trim()}
                    onClick={() => onSubmit(reason.trim())}
                    className="rounded bg-primary px-2 py-1 text-xs font-medium text-white hover:bg-primary-hover disabled:opacity-50"
                >
                    Dismiss
                </button>
                <button
                    type="button"
                    onClick={onCancel}
                    className="rounded border border-border bg-card px-2 py-1 text-xs hover:bg-card-hover"
                >
                    Cancel
                </button>
            </div>
        </div>
    );
}

function ReassignForm({
    users,
    currentAssignee,
    onCancel,
    onSubmit,
}: {
    users: { id: string; name: string | null }[];
    currentAssignee: string | null;
    onCancel: () => void;
    onSubmit: (userId: string | null) => void;
}) {
    const [selected, setSelected] = useState<string>(currentAssignee ?? "");
    return (
        <div className="rounded border border-border-light bg-bg-secondary p-2 text-sm">
            <select
                value={selected}
                onChange={(e) => setSelected(e.target.value)}
                className="w-full rounded border border-border bg-card px-2 py-1 text-sm text-text"
            >
                <option value="">— Unassigned —</option>
                {users.map((u) => (
                    <option key={u.id} value={u.id}>
                        {u.name || u.id}
                    </option>
                ))}
            </select>
            <div className="mt-2 flex gap-2">
                <button
                    type="button"
                    onClick={() => onSubmit(selected || null)}
                    className="rounded bg-primary px-2 py-1 text-xs font-medium text-white hover:bg-primary-hover"
                >
                    Assign
                </button>
                <button
                    type="button"
                    onClick={onCancel}
                    className="rounded border border-border bg-card px-2 py-1 text-xs hover:bg-card-hover"
                >
                    Cancel
                </button>
            </div>
        </div>
    );
}

function ReturnForm({
    onCancel,
    onSubmit,
}: {
    onCancel: () => void;
    onSubmit: (reason: string) => void;
}) {
    const [reason, setReason] = useState("");
    return (
        <div className="rounded border border-border-light bg-bg-secondary p-2 text-sm">
            <p className="mb-1 text-xs text-text-muted">
                Returning sends this item back to the rep on the source call so they
                can redirect or handle it themselves. Add a quick note.
            </p>
            <textarea
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                placeholder="Why are you returning this?"
                rows={2}
                className="w-full rounded border border-border bg-card px-2 py-1 text-sm text-text"
            />
            <div className="mt-2 flex gap-2">
                <button
                    type="button"
                    disabled={!reason.trim()}
                    onClick={() => onSubmit(reason.trim())}
                    className="rounded bg-accent-amber px-2 py-1 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50"
                >
                    Return
                </button>
                <button
                    type="button"
                    onClick={onCancel}
                    className="rounded border border-border bg-card px-2 py-1 text-xs hover:bg-card-hover"
                >
                    Cancel
                </button>
            </div>
        </div>
    );
}

// ── Helpers ─────────────────────────────────────────────────────────────

function channelIconFor(channel: string | null): string {
    switch (channel) {
        case "meeting":
            return "🗓";
        case "phone_call":
            return "📞";
        case "email":
            return "✉";
        case "document_send":
            return "📎";
        default:
            return "•";
    }
}

function DueDatePill({ due }: { due: string }) {
    const date = new Date(due);
    const overdue = date.getTime() < Date.now();
    return (
        <span
            className={`whitespace-nowrap rounded px-1.5 py-0.5 text-xs ${
                overdue
                    ? "bg-accent-rose/30 text-text"
                    : "bg-primary-soft text-text"
            }`}
            title={`Due ${date.toLocaleDateString()}`}
        >
            {date.toLocaleDateString(undefined, {
                month: "short",
                day: "numeric",
            })}
        </span>
    );
}

function PriorityDot({ priority }: { priority: string }) {
    const color =
        priority === "high"
            ? "bg-accent-rose"
            : priority === "low"
                ? "bg-text-subtle"
                : "bg-accent-amber";
    return (
        <span
            aria-label={`${priority} priority`}
            title={`${priority} priority`}
            className={`inline-block h-2 w-2 rounded-full ${color}`}
        />
    );
}

function FeedbackButtons({
    score,
    onClick,
}: {
    score: number;
    onClick: (helpful: boolean) => void;
}) {
    return (
        <div className="ml-auto flex items-center gap-1 text-xs text-text-muted">
            <span title={`Feedback score: ${score}`}>{score}</span>
            <button
                type="button"
                aria-label="Helpful"
                onClick={() => onClick(true)}
                className="rounded p-1 hover:bg-card-hover"
            >
                ▲
            </button>
            <button
                type="button"
                aria-label="Not helpful"
                onClick={() => onClick(false)}
                className="rounded p-1 hover:bg-card-hover"
            >
                ▼
            </button>
        </div>
    );
}
