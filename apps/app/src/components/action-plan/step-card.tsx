"use client";

import { useState } from "react";

import {
    ActionPlan,
    ActionStep,
    ActionStepState,
    type StepEditPayload,
    useCompleteStep,
    useDeleteStep,
    useEditStep,
    useRestoreStep,
    useScheduleStepMeeting,
    useSkipStep,
} from "@/lib/action-plans";
import { useCalendarProviders } from "@/lib/oauth";
import { NoteInput } from "./note-input";
import { ResponseThread } from "./response-thread";

interface StepCardProps {
    plan: ActionPlan;
    step: ActionStep;
    highlightAsEndpoint?: boolean;
}

const STATE_LABEL: Record<ActionStepState, string> = {
    blocked: "Blocked",
    ready: "Ready",
    in_progress: "In progress",
    awaiting_response: "Awaiting response",
    done: "Done",
    skipped: "Skipped",
    deleted: "Deleted",
};

const STATE_CLASSES: Record<ActionStepState, string> = {
    blocked: "bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300",
    ready: "bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-200",
    in_progress: "bg-blue-100 text-blue-800 dark:bg-blue-900/40 dark:text-blue-200",
    awaiting_response: "bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-200",
    done: "bg-green-200 text-green-900 dark:bg-green-900/60 dark:text-green-100",
    skipped: "bg-slate-200 text-slate-500 line-through dark:bg-slate-800 dark:text-slate-500",
    deleted: "bg-rose-100 text-rose-800 dark:bg-rose-900/40 dark:text-rose-200",
};

function priorityDot(priority: string): string {
    if (priority === "high") return "bg-rose-500";
    if (priority === "low") return "bg-slate-400";
    return "bg-amber-500";
}

function channelIcon(channel: string | null): string {
    switch (channel) {
        case "email":
            return "✉";
        case "phone_call":
            return "☎";
        case "meeting":
            return "▥";
        case "research":
            return "⌕";
        case "document_send":
            return "❒";
        case "system_write":
            return "⚙";
        case "note":
        default:
            return "✎";
    }
}

export function StepCard({ plan, step, highlightAsEndpoint }: StepCardProps) {
    const [expanded, setExpanded] = useState(
        highlightAsEndpoint || step.state === "ready" || step.state === "awaiting_response",
    );
    const complete = useCompleteStep(plan.id);
    const skip = useSkipStep(plan.id);
    const del = useDeleteStep(plan.id);
    const restore = useRestoreStep(plan.id);
    const edit = useEditStep(plan.id);
    const schedule = useScheduleStepMeeting(plan.id);
    // Pre-flight which calendar provider would serve a Schedule click.
    // Same gate as the legacy ActionItem card: connected provider →
    // Schedule button; no provider → Connect-calendar CTA.
    const calendarProviders = useCalendarProviders();
    const hasRealCalendarProvider = Boolean(
        calendarProviders.data?.active_provider,
    );
    const isMeetingStep =
        step.recommended_channel === "meeting" ||
        step.recommended_channel === "phone_call";
    const [editing, setEditing] = useState(false);
    const [scheduleResult, setScheduleResult] = useState<{
        ok: boolean;
        note: string;
        joinUrl?: string | null;
        ics?: string | null;
    } | null>(null);
    const isTerminal = ["done", "skipped", "deleted"].includes(step.state);

    return (
        <div
            className={[
                "rounded border p-3 transition-shadow",
                highlightAsEndpoint
                    ? "border-2 border-indigo-500 shadow-md dark:border-indigo-400"
                    : "border-slate-200 dark:border-slate-700",
                step.state === "skipped" ? "opacity-60" : "",
            ].join(" ")}
        >
            <div className="flex items-start gap-2">
                <button
                    type="button"
                    aria-label={isTerminal ? "Re-open step" : "Mark step done"}
                    className={[
                        "mt-0.5 h-5 w-5 shrink-0 rounded border",
                        step.state === "done"
                            ? "border-green-600 bg-green-600 text-white"
                            : "border-slate-400 dark:border-slate-500",
                    ].join(" ")}
                    onClick={() => {
                        if (step.state === "done" || step.state === "skipped") return;
                        complete.mutate({ stepId: step.id });
                    }}
                >
                    {step.state === "done" ? "✓" : ""}
                </button>
                <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                        <span
                            className={`inline-block h-2 w-2 rounded-full ${priorityDot(step.priority)}`}
                            aria-label={`priority ${step.priority}`}
                        />
                        <span className="font-mono text-xs text-slate-500">
                            {channelIcon(step.recommended_channel)}
                        </span>
                        <h3 className="truncate text-sm font-semibold">{step.title}</h3>
                        <span
                            className={`ml-auto rounded px-1.5 py-0.5 text-[10px] uppercase tracking-wide ${STATE_CLASSES[step.state] ?? STATE_CLASSES.ready}`}
                        >
                            {STATE_LABEL[step.state] ?? step.state}
                        </span>
                    </div>
                    {step.description && (
                        <p className="mt-1 text-xs text-slate-600 dark:text-slate-300">
                            {step.description}
                        </p>
                    )}
                    {step.implicit_signal && (
                        <p className="mt-2 rounded bg-amber-50 px-2 py-1 text-xs text-amber-900 dark:bg-amber-900/30 dark:text-amber-100">
                            <span className="font-medium">Note from the call: </span>
                            {step.implicit_signal}
                        </p>
                    )}
                    {step.artifact_stale && (
                        <p className="mt-2 text-[11px] italic text-slate-500 dark:text-slate-400">
                            Draft updating with the latest reply data…
                        </p>
                    )}
                    {expanded && (
                        <div className="mt-3 space-y-3">
                            {step.kb_source && (
                                <p className="text-[11px] text-slate-500 dark:text-slate-400">
                                    Grounded in KB chunk
                                    {step.compliance_level
                                        ? ` (compliance: ${step.compliance_level})`
                                        : ""}
                                    .
                                </p>
                            )}
                            {step.channel_reasoning && (
                                <p className="text-xs text-slate-600 dark:text-slate-300">
                                    <span className="font-medium">Why this channel: </span>
                                    {step.channel_reasoning}
                                </p>
                            )}
                            <ParticipantsBlock step={step} />
                            <PrepArtifactsBlock step={step} />
                            <ArtifactBlock step={step} />
                            <ResponseThread step={step} />
                            <NoteInput plan={plan} step={step} />
                            <div className="flex flex-wrap gap-2 pt-2">
                                {!isTerminal && isMeetingStep && hasRealCalendarProvider && (
                                    <button
                                        type="button"
                                        disabled={schedule.isPending}
                                        className="rounded bg-indigo-600 px-2 py-1 text-xs font-medium text-white hover:bg-indigo-700 disabled:opacity-50"
                                        onClick={async () => {
                                            setScheduleResult(null);
                                            try {
                                                const result = await schedule.mutateAsync({
                                                    stepId: step.id,
                                                    payload: {},
                                                });
                                                if (result.success) {
                                                    setScheduleResult({
                                                        ok: true,
                                                        note: result.join_url
                                                            ? `Created. Join: ${result.join_url}`
                                                            : result.note ?? "Created (calendar invite ready).",
                                                        joinUrl: result.join_url,
                                                        ics: result.ics_payload,
                                                    });
                                                } else {
                                                    setScheduleResult({
                                                        ok: false,
                                                        note: `Failed: ${result.error ?? "unknown error"}`,
                                                    });
                                                }
                                            } catch (err) {
                                                setScheduleResult({
                                                    ok: false,
                                                    note: `Failed: ${(err as Error).message}`,
                                                });
                                            }
                                        }}
                                    >
                                        {step.recommended_channel === "phone_call"
                                            ? "Schedule call"
                                            : "Schedule meeting"}
                                    </button>
                                )}
                                {!isTerminal && isMeetingStep && !hasRealCalendarProvider && !calendarProviders.isLoading && (
                                    <a
                                        href="/settings#integrations"
                                        title="No calendar connected. Connect Google Calendar or Microsoft to schedule directly from action plans."
                                        className="rounded border border-amber-500 bg-amber-50 px-2 py-1 text-xs font-medium text-amber-800 hover:bg-amber-100 dark:border-amber-600 dark:bg-amber-900/30 dark:text-amber-200"
                                    >
                                        Connect a calendar
                                    </a>
                                )}
                                {!isTerminal && (
                                    <>
                                        <button
                                            type="button"
                                            className="rounded border border-slate-300 px-2 py-1 text-xs dark:border-slate-600"
                                            onClick={() => setEditing((v) => !v)}
                                        >
                                            {editing ? "Cancel edit" : "Edit"}
                                        </button>
                                        <button
                                            type="button"
                                            className="rounded border border-slate-300 px-2 py-1 text-xs dark:border-slate-600"
                                            onClick={() => skip.mutate({ stepId: step.id })}
                                        >
                                            Skip
                                        </button>
                                        <button
                                            type="button"
                                            className="rounded border border-rose-300 px-2 py-1 text-xs text-rose-700 dark:border-rose-700 dark:text-rose-200"
                                            onClick={() => {
                                                if (window.confirm("Delete this step? Downstream steps will be re-evaluated.")) {
                                                    del.mutate({ stepId: step.id });
                                                }
                                            }}
                                        >
                                            Delete
                                        </button>
                                    </>
                                )}
                                {step.state === "skipped" && (
                                    <button
                                        type="button"
                                        className="rounded border border-slate-300 bg-slate-50 px-2 py-1 text-xs text-slate-700 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-200"
                                        onClick={() => restore.mutate({ stepId: step.id })}
                                    >
                                        Unskip
                                    </button>
                                )}
                            </div>
                            {scheduleResult && (
                                <div
                                    className={
                                        scheduleResult.ok
                                            ? "rounded border border-emerald-300 bg-emerald-50 p-2 text-xs text-emerald-900 dark:border-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-100"
                                            : "rounded border border-rose-300 bg-rose-50 p-2 text-xs text-rose-900 dark:border-rose-700 dark:bg-rose-900/30 dark:text-rose-100"
                                    }
                                >
                                    <p>{scheduleResult.note}</p>
                                    {scheduleResult.joinUrl && (
                                        <p className="mt-1">
                                            <a
                                                href={scheduleResult.joinUrl}
                                                target="_blank"
                                                rel="noreferrer"
                                                className="underline"
                                            >
                                                Open meeting
                                            </a>
                                        </p>
                                    )}
                                    {scheduleResult.ics && (
                                        <details className="mt-1">
                                            <summary className="cursor-pointer">ICS payload (copy/paste into your calendar)</summary>
                                            <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap rounded bg-slate-100 p-2 text-[10px] dark:bg-slate-800">{scheduleResult.ics}</pre>
                                        </details>
                                    )}
                                </div>
                            )}
                            {editing && !isTerminal && (
                                <EditStepForm
                                    step={step}
                                    onCancel={() => setEditing(false)}
                                    onSave={(patch) => {
                                        edit.mutate(
                                            { stepId: step.id, patch },
                                            { onSuccess: () => setEditing(false) },
                                        );
                                    }}
                                    pending={edit.isPending}
                                />
                            )}
                        </div>
                    )}
                    <button
                        type="button"
                        className="mt-2 text-xs text-indigo-600 hover:underline dark:text-indigo-400"
                        onClick={() => setExpanded((v) => !v)}
                    >
                        {expanded ? "Collapse" : "Expand"}
                    </button>
                </div>
            </div>
        </div>
    );
}

function ParticipantsBlock({ step }: { step: ActionStep }) {
    if (!step.participants || step.participants.length === 0) return null;
    return (
        <div>
            <h4 className="text-[11px] font-semibold uppercase tracking-wide text-slate-500">
                Participants
            </h4>
            <ul className="mt-1 space-y-0.5 text-xs">
                {step.participants.map((p, i) => (
                    <li key={i}>
                        <span className="font-medium">{p.name ?? "(unnamed)"}</span>
                        {p.role ? <span className="text-slate-500">. {p.role}</span> : null}
                        {p.side ? (
                            <span className="ml-1 rounded bg-slate-100 px-1 text-[10px] uppercase text-slate-600 dark:bg-slate-800 dark:text-slate-300">
                                {p.side}
                            </span>
                        ) : null}
                    </li>
                ))}
            </ul>
        </div>
    );
}

function PrepArtifactsBlock({ step }: { step: ActionStep }) {
    if (!step.prep_artifacts || step.prep_artifacts.length === 0) return null;
    return (
        <div>
            <h4 className="text-[11px] font-semibold uppercase tracking-wide text-slate-500">
                Prep checklist
            </h4>
            <ul className="mt-1 list-inside list-disc space-y-0.5 text-xs">
                {step.prep_artifacts.map((p, i) => (
                    <li key={i}>{String(p)}</li>
                ))}
            </ul>
        </div>
    );
}

function ArtifactBlock({ step }: { step: ActionStep }) {
    const a = step.latest_artifact;
    if (!a) return null;
    const payload = a.payload as Record<string, unknown>;
    // Human heading per artifact kind. Internal metadata (kind code,
    // version number, model tier) is no longer surfaced to the rep.
    const heading = a.kind === "email"
        ? "Email draft"
        : a.kind === "script"
          ? "Call script"
          : a.kind === "system_write_payload"
            ? "System update"
            : "Draft";
    return (
        <div>
            <h4 className="text-[11px] font-semibold uppercase tracking-wide text-slate-500">
                {heading}
            </h4>
            {a.kind === "email" || a.kind === "system_write_payload" ? (
                <div className="mt-1 space-y-1 rounded border border-slate-200 p-2 font-mono text-[11px] dark:border-slate-700">
                    {payload.subject ? (
                        <div>
                            <span className="font-semibold">Subject: </span>
                            {String(payload.subject)}
                        </div>
                    ) : null}
                    <pre className="whitespace-pre-wrap text-[11px]">
                        {String(payload.body ?? JSON.stringify(payload.payload ?? payload, null, 2))}
                    </pre>
                </div>
            ) : a.kind === "script" ? (
                <div className="mt-1 space-y-1 rounded border border-slate-200 p-2 text-xs dark:border-slate-700">
                    {payload.opening_line ? <p>{String(payload.opening_line)}</p> : null}
                    {Array.isArray(payload.bullets) && (
                        <ul className="list-inside list-disc">
                            {(payload.bullets as unknown[]).map((b, i) => (
                                <li key={i}>{String(b)}</li>
                            ))}
                        </ul>
                    )}
                    {payload.closing_line ? <p>{String(payload.closing_line)}</p> : null}
                </div>
            ) : (
                <pre className="mt-1 whitespace-pre-wrap rounded border border-slate-200 p-2 font-mono text-[11px] dark:border-slate-700">
                    {JSON.stringify(payload, null, 2)}
                </pre>
            )}
        </div>
    );
}

/**
 * Inline editor for a step's user-facing fields.
 *
 * Pre-filled from the current step. Each save writes a row to
 * step_feedback_logs on the backend so the synthesizer can adapt
 * THIS user's future plans toward their preferred phrasing /
 * channel / priority.
 */
function EditStepForm({
    step,
    onCancel,
    onSave,
    pending,
}: {
    step: ActionStep;
    onCancel: () => void;
    onSave: (patch: StepEditPayload) => void;
    pending: boolean;
}) {
    const [title, setTitle] = useState(step.title);
    const [description, setDescription] = useState(step.description ?? "");
    const [priority, setPriority] = useState<"high" | "medium" | "low">(
        (step.priority as "high" | "medium" | "low") || "medium",
    );
    const [dueDate, setDueDate] = useState<string>(
        step.due_date ? String(step.due_date).slice(0, 10) : "",
    );
    const [channel, setChannel] = useState<string>(step.recommended_channel ?? "");

    const channelOptions = [
        { value: "", label: "(none)" },
        { value: "email", label: "Email" },
        { value: "phone_call", label: "Phone call" },
        { value: "meeting", label: "Meeting" },
        { value: "document_send", label: "Document send" },
        { value: "research", label: "Research" },
        { value: "system_write", label: "System update" },
        { value: "note", label: "Note" },
    ];

    function handleSubmit(e: React.FormEvent) {
        e.preventDefault();
        const patch: StepEditPayload = {};
        if (title !== step.title) patch.title = title;
        if (description !== (step.description ?? "")) patch.description = description;
        if (priority !== step.priority) patch.priority = priority;
        if (channel !== (step.recommended_channel ?? "")) {
            patch.recommended_channel = channel || undefined;
        }
        const currentDue = step.due_date ? String(step.due_date).slice(0, 10) : "";
        if (dueDate !== currentDue) {
            patch.due_date = dueDate;
        }
        if (Object.keys(patch).length === 0) {
            onCancel();
            return;
        }
        onSave(patch);
    }

    return (
        <form
            onSubmit={handleSubmit}
            className="space-y-2 rounded border border-slate-300 bg-slate-50 p-3 text-xs dark:border-slate-600 dark:bg-slate-900"
        >
            <label className="block">
                <span className="text-text-subtle">Title</span>
                <input
                    type="text"
                    value={title}
                    onChange={(e) => setTitle(e.target.value)}
                    className="mt-0.5 w-full rounded border border-slate-300 bg-white px-2 py-1 text-sm dark:border-slate-600 dark:bg-slate-800"
                />
            </label>
            <label className="block">
                <span className="text-text-subtle">Description</span>
                <textarea
                    value={description}
                    onChange={(e) => setDescription(e.target.value)}
                    rows={2}
                    className="mt-0.5 w-full rounded border border-slate-300 bg-white px-2 py-1 text-sm dark:border-slate-600 dark:bg-slate-800"
                />
            </label>
            <div className="grid grid-cols-3 gap-2">
                <label className="block">
                    <span className="text-text-subtle">Priority</span>
                    <select
                        value={priority}
                        onChange={(e) => setPriority(e.target.value as typeof priority)}
                        className="mt-0.5 w-full rounded border border-slate-300 bg-white px-2 py-1 text-sm dark:border-slate-600 dark:bg-slate-800"
                    >
                        <option value="high">High</option>
                        <option value="medium">Medium</option>
                        <option value="low">Low</option>
                    </select>
                </label>
                <label className="block">
                    <span className="text-text-subtle">Channel</span>
                    <select
                        value={channel}
                        onChange={(e) => setChannel(e.target.value)}
                        className="mt-0.5 w-full rounded border border-slate-300 bg-white px-2 py-1 text-sm dark:border-slate-600 dark:bg-slate-800"
                    >
                        {channelOptions.map((o) => (
                            <option key={o.value} value={o.value}>
                                {o.label}
                            </option>
                        ))}
                    </select>
                </label>
                <label className="block">
                    <span className="text-text-subtle">Due date</span>
                    <input
                        type="date"
                        value={dueDate}
                        onChange={(e) => setDueDate(e.target.value)}
                        className="mt-0.5 w-full rounded border border-slate-300 bg-white px-2 py-1 text-sm dark:border-slate-600 dark:bg-slate-800"
                    />
                </label>
            </div>
            <p className="text-[10px] text-text-subtle">
                Edits save to your personal feedback log. Linda will lean toward
                this shape on your future plans.
            </p>
            <div className="flex justify-end gap-2">
                <button
                    type="button"
                    onClick={onCancel}
                    className="rounded border border-slate-300 px-2 py-1 text-xs dark:border-slate-600"
                >
                    Cancel
                </button>
                <button
                    type="submit"
                    disabled={pending}
                    className="rounded bg-indigo-600 px-2 py-1 text-xs font-medium text-white hover:bg-indigo-700 disabled:opacity-50"
                >
                    {pending ? "Saving…" : "Save"}
                </button>
            </div>
        </form>
    );
}
