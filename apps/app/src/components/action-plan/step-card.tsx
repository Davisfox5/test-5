"use client";

import { useState } from "react";

import {
    ActionPlan,
    ActionStep,
    ActionStepState,
    type GenerateDocumentResult,
    type StepEditPayload,
    useCommitStep,
    useCompleteStep,
    useDeleteStep,
    useEditStep,
    useGenerateStepDocument,
    useMarkStepSent,
    useRestoreStep,
    useScheduleStepMeeting,
    useSendStepEmail,
    useSkipStep,
    useStepResolved,
} from "@/lib/action-plans";
import { useCalendarProviders, useEmailProviders } from "@/lib/oauth";
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
    const sendEmail = useSendStepEmail(plan.id);
    const commit = useCommitStep(plan.id);
    const markSent = useMarkStepSent(plan.id);
    const generateDoc = useGenerateStepDocument(plan.id);
    const [generatedDoc, setGeneratedDoc] = useState<GenerateDocumentResult | null>(null);
    const [docError, setDocError] = useState<string | null>(null);
    // Pre-flight which calendar / email provider would serve.
    const calendarProviders = useCalendarProviders();
    const emailProviders = useEmailProviders();
    const hasRealCalendarProvider = Boolean(
        calendarProviders.data?.active_provider,
    );
    const hasEmailProvider = Boolean(emailProviders.data?.active_provider);

    const isMeetingStep =
        step.recommended_channel === "meeting" ||
        step.recommended_channel === "phone_call";
    const isPhoneStep = step.recommended_channel === "phone_call";
    const isEmailStep =
        step.recommended_channel === "email" ||
        step.recommended_channel === "document_send";
    const isDocumentStep = step.recommended_channel === "document_send";
    const isNoteStep = step.recommended_channel === "note";
    const isSystemWriteStep = step.recommended_channel === "system_write";
    const isCommittableStep = isNoteStep || isSystemWriteStep;

    const [editing, setEditing] = useState(false);
    const [scheduleResult, setScheduleResult] = useState<{
        ok: boolean;
        note: string;
        joinUrl?: string | null;
        ics?: string | null;
    } | null>(null);
    const [sendResult, setSendResult] = useState<{
        ok: boolean;
        note: string;
    } | null>(null);
    const [commitResult, setCommitResult] = useState<{
        ok: boolean;
        note: string;
    } | null>(null);
    const isTerminal = ["done", "skipped", "deleted"].includes(step.state);
    // Lazy-load resolved attachments + participants only when the
    // card is expanded — saves the round-trip when the user just
    // scrolls past collapsed cards.
    const resolved = useStepResolved(plan.id, step.id);

    // Outbound-channel steps in a non-terminal state get a "mark sent
    // manually" affordance so reps can close the loop when they used
    // an out-of-app channel (sent from Gmail-on-phone, dialed from
    // their desk phone, etc.).
    const supportsManualSent =
        !isTerminal && (isEmailStep || isPhoneStep || isMeetingStep || isNoteStep);

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
                                {!isTerminal && isEmailStep && hasEmailProvider && (
                                    <button
                                        type="button"
                                        disabled={sendEmail.isPending}
                                        className="rounded bg-emerald-600 px-2 py-1 text-xs font-medium text-white hover:bg-emerald-700 disabled:opacity-50"
                                        onClick={async () => {
                                            setSendResult(null);
                                            try {
                                                const result = await sendEmail.mutateAsync({
                                                    stepId: step.id,
                                                    payload: {},
                                                });
                                                setSendResult({
                                                    ok: result.success,
                                                    note: result.success
                                                        ? `Sent via ${result.provider}. Message id: ${result.provider_message_id ?? "(none)"}`
                                                        : `Send failed: ${result.error ?? "unknown error"}`,
                                                });
                                            } catch (err) {
                                                setSendResult({
                                                    ok: false,
                                                    note: `Send failed: ${(err as Error).message}`,
                                                });
                                            }
                                        }}
                                    >
                                        {step.recommended_channel === "document_send"
                                            ? "Send document"
                                            : "Send email"}
                                    </button>
                                )}
                                {!isTerminal && isEmailStep && !hasEmailProvider && !emailProviders.isLoading && (
                                    <a
                                        href="/settings#integrations"
                                        title="Connect Gmail or Outlook to send directly from action plans."
                                        className="rounded border border-amber-500 bg-amber-50 px-2 py-1 text-xs font-medium text-amber-800 hover:bg-amber-100 dark:border-amber-600 dark:bg-amber-900/30 dark:text-amber-200"
                                    >
                                        Connect email
                                    </a>
                                )}
                                {!isTerminal && isDocumentStep && (
                                    <button
                                        type="button"
                                        disabled={generateDoc.isPending}
                                        className="rounded bg-fuchsia-600 px-2 py-1 text-xs font-medium text-white hover:bg-fuchsia-700 disabled:opacity-50"
                                        onClick={async () => {
                                            setDocError(null);
                                            setGeneratedDoc(null);
                                            try {
                                                const result = await generateDoc.mutateAsync({
                                                    stepId: step.id,
                                                    payload: {},
                                                });
                                                setGeneratedDoc(result);
                                            } catch (err) {
                                                setDocError((err as Error).message);
                                            }
                                        }}
                                        title="Run Claude Sonnet against this step + the source call to draft a full document body. Returns Markdown for review, download, or print."
                                    >
                                        {generateDoc.isPending ? "Generating…" : "Generate document"}
                                    </button>
                                )}
                                {!isTerminal && isPhoneStep && resolved.data && (
                                    <PhoneCallButton resolved={resolved.data} />
                                )}
                                {!isTerminal && isCommittableStep && (
                                    <button
                                        type="button"
                                        disabled={commit.isPending}
                                        className="rounded bg-indigo-600 px-2 py-1 text-xs font-medium text-white hover:bg-indigo-700 disabled:opacity-50"
                                        onClick={async () => {
                                            setCommitResult(null);
                                            try {
                                                const result = await commit.mutateAsync({
                                                    stepId: step.id,
                                                    payload: {},
                                                });
                                                setCommitResult({
                                                    ok: result.success,
                                                    note: result.success
                                                        ? (isSystemWriteStep
                                                            ? `Operation executed in ${result.provider}. id: ${result.external_id ?? "(none)"}`
                                                            : `Note created in ${result.provider}. id: ${result.external_id ?? "(unanchored)"}`)
                                                        : `Commit failed: ${result.error ?? "unknown error"}`,
                                                });
                                            } catch (err) {
                                                setCommitResult({
                                                    ok: false,
                                                    note: `Commit failed: ${(err as Error).message}`,
                                                });
                                            }
                                        }}
                                        title={
                                            isSystemWriteStep
                                                ? "Execute the synthesizer-emitted system_write operation against the connected CRM."
                                                : "Push this note into the connected CRM (HubSpot / Salesforce / Pipedrive)."
                                        }
                                    >
                                        {isSystemWriteStep ? "Execute in CRM" : "Send to CRM"}
                                    </button>
                                )}
                                {supportsManualSent && (
                                    <button
                                        type="button"
                                        disabled={markSent.isPending}
                                        className="rounded border border-slate-300 px-2 py-1 text-xs text-slate-700 dark:border-slate-600 dark:text-slate-200"
                                        onClick={() => {
                                            const note = window.prompt(
                                                "Mark this step done. How did you complete it? (e.g. 'sent from Gmail on phone', 'called from desk phone')",
                                                "",
                                            );
                                            if (note === null) return;
                                            markSent.mutate({
                                                stepId: step.id,
                                                payload: { source: "manual_close", note: note || null },
                                            });
                                        }}
                                        title="Already took this action outside the app? Record it here to advance the plan."
                                    >
                                        Mark done manually
                                    </button>
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
                            {sendResult && (
                                <p
                                    className={
                                        sendResult.ok
                                            ? "rounded border border-emerald-300 bg-emerald-50 p-2 text-xs text-emerald-900 dark:border-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-100"
                                            : "rounded border border-rose-300 bg-rose-50 p-2 text-xs text-rose-900 dark:border-rose-700 dark:bg-rose-900/30 dark:text-rose-100"
                                    }
                                >
                                    {sendResult.note}
                                </p>
                            )}
                            {commitResult && (
                                <p
                                    className={
                                        commitResult.ok
                                            ? "rounded border border-emerald-300 bg-emerald-50 p-2 text-xs text-emerald-900 dark:border-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-100"
                                            : "rounded border border-rose-300 bg-rose-50 p-2 text-xs text-rose-900 dark:border-rose-700 dark:bg-rose-900/30 dark:text-rose-100"
                                    }
                                >
                                    {commitResult.note}
                                </p>
                            )}
                            <ResolvedBlock resolved={resolved.data} isLoading={resolved.isLoading} />
                            {docError && (
                                <p className="rounded border border-rose-300 bg-rose-50 p-2 text-xs text-rose-900 dark:border-rose-700 dark:bg-rose-900/30 dark:text-rose-100">
                                    Document generation failed: {docError}
                                </p>
                            )}
                            {generatedDoc && (
                                <GeneratedDocumentPanel
                                    doc={generatedDoc}
                                    onDismiss={() => setGeneratedDoc(null)}
                                />
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

// Phone-call step: click-to-call via the rep's native dialer. Resolves
// the customer's phone from the first customer-side participant with a
// number. Outbound-call placement itself is intentionally out of
// scope (see backend/app/services/telephony/__init__.py: call control
// lives in the tenant's phone system, not LINDA).
function PhoneCallButton({
    resolved,
}: {
    resolved: import("@/lib/action-plans").StepResolved;
}) {
    // Prefer the first customer-side participant with a phone. Fall
    // back to a blank tel: link when no number's on file so the rep
    // still gets a one-click dialer open.
    const customer = resolved.participants?.find(
        (p) => p.side === "customer" && p.phone,
    );
    const customerNameForTooltip = customer?.name
        ?? resolved.participants?.find((p) => p.side === "customer")?.name;
    const href = customer?.phone
        ? `tel:${customer.phone.replace(/\s+/g, "")}`
        : "tel:";
    return (
        <a
            href={href}
            className="rounded bg-sky-600 px-2 py-1 text-xs font-medium text-white hover:bg-sky-700"
            title={
                customer?.phone
                    ? `Place call to ${customer.name} at ${customer.phone}.`
                    : customerNameForTooltip
                        ? `Place call to ${customerNameForTooltip} (no number on file — dialer opens blank).`
                        : "Open your dialer to place this call."
            }
        >
            Place call
        </a>
    );
}

// Renders attachment links (resolved to real KB doc URLs when matched)
// and the resolved participant list with emails. Both fall back
// gracefully when the resolver returns nothing or a name didn't match.
function ResolvedBlock({
    resolved,
    isLoading,
}: {
    resolved: import("@/lib/action-plans").StepResolved | undefined;
    isLoading: boolean;
}) {
    if (isLoading) {
        return (
            <p className="text-[11px] text-slate-400 dark:text-slate-500">
                Resolving attachments and participants…
            </p>
        );
    }
    if (!resolved) return null;
    const hasAttachments = (resolved.attachments?.length ?? 0) > 0;
    const hasParticipants = (resolved.participants?.length ?? 0) > 0;
    if (!hasAttachments && !hasParticipants) return null;
    return (
        <div className="space-y-2 rounded border border-slate-200 bg-slate-50 p-2 text-xs dark:border-slate-700 dark:bg-slate-800/50">
            {hasAttachments && (
                <div>
                    <div className="font-medium text-slate-700 dark:text-slate-200">
                        Attachments
                    </div>
                    <ul className="mt-1 space-y-1">
                        {resolved.attachments.map((att, idx) => (
                            <li key={idx} className="flex flex-wrap items-baseline gap-1">
                                {att.kb_doc_id && att.source_url ? (
                                    <a
                                        href={att.source_url}
                                        target="_blank"
                                        rel="noreferrer"
                                        className="font-medium text-indigo-700 underline dark:text-indigo-300"
                                    >
                                        {att.title}
                                    </a>
                                ) : att.kb_doc_id ? (
                                    <a
                                        href={`/knowledge-base#${att.kb_doc_id}`}
                                        className="font-medium text-indigo-700 underline dark:text-indigo-300"
                                    >
                                        {att.title}
                                    </a>
                                ) : (
                                    <span className="font-medium text-slate-700 dark:text-slate-200">
                                        {att.title}
                                    </span>
                                )}
                                {att.reason && (
                                    <span className="text-slate-500 dark:text-slate-400">
                                        — {att.reason}
                                    </span>
                                )}
                                {!att.kb_doc_id && (
                                    <span className="rounded bg-amber-100 px-1 text-[10px] text-amber-800 dark:bg-amber-900/40 dark:text-amber-200">
                                        no KB match
                                    </span>
                                )}
                            </li>
                        ))}
                    </ul>
                </div>
            )}
            {hasParticipants && (
                <div>
                    <div className="font-medium text-slate-700 dark:text-slate-200">
                        Participants
                    </div>
                    <ul className="mt-1 space-y-0.5">
                        {resolved.participants.map((p, idx) => (
                            <li key={idx} className="flex flex-wrap items-baseline gap-1">
                                <span className="font-medium text-slate-700 dark:text-slate-200">
                                    {p.name}
                                </span>
                                {p.role && (
                                    <span className="text-slate-500 dark:text-slate-400">
                                        ({p.role})
                                    </span>
                                )}
                                {p.email ? (
                                    <a
                                        href={`mailto:${p.email}`}
                                        className="text-indigo-700 underline dark:text-indigo-300"
                                    >
                                        {p.email}
                                    </a>
                                ) : (
                                    <span className="rounded bg-amber-100 px-1 text-[10px] text-amber-800 dark:bg-amber-900/40 dark:text-amber-200">
                                        email pending
                                    </span>
                                )}
                                {p.phone && (
                                    <a
                                        href={`tel:${p.phone.replace(/\s+/g, "")}`}
                                        className="text-indigo-700 underline dark:text-indigo-300"
                                    >
                                        {p.phone}
                                    </a>
                                )}
                                {p.side && (
                                    <span className="text-[10px] text-slate-400">
                                        [{p.side}]
                                    </span>
                                )}
                            </li>
                        ))}
                    </ul>
                </div>
            )}
        </div>
    );
}

// Minimal Markdown → HTML for the LLM-emitted document body.
// Handles the subset the system prompt asks for: ATX headings (# / ## /
// ###), paragraphs, bulleted lists (- prefix), numbered lists, and
// inline **bold** / *italic*. We intentionally do NOT pull in a real
// markdown library — the AI output is constrained and a 30-line
// converter is easier to audit than 200KB of dependency.
function renderMarkdownToHtml(md: string): string {
    const escape = (s: string) =>
        s
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;");
    const inline = (s: string) =>
        escape(s)
            .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
            .replace(/(^|\W)\*(\S(?:.*?\S)?)\*(?=\W|$)/g, "$1<em>$2</em>");

    const lines = md.replace(/\r\n/g, "\n").split("\n");
    const out: string[] = [];
    let inList: null | "ul" | "ol" = null;
    let paragraph: string[] = [];

    function flushParagraph() {
        if (paragraph.length) {
            out.push(`<p>${inline(paragraph.join(" "))}</p>`);
            paragraph = [];
        }
    }
    function flushList() {
        if (inList) {
            out.push(`</${inList}>`);
            inList = null;
        }
    }

    for (const raw of lines) {
        const line = raw.trimEnd();
        if (!line.trim()) {
            flushParagraph();
            flushList();
            continue;
        }
        const heading = /^(#{1,3})\s+(.+)$/.exec(line);
        if (heading) {
            flushParagraph();
            flushList();
            const level = heading[1].length;
            out.push(`<h${level}>${inline(heading[2])}</h${level}>`);
            continue;
        }
        const ul = /^[-*]\s+(.+)$/.exec(line.trimStart());
        if (ul) {
            flushParagraph();
            if (inList !== "ul") {
                flushList();
                out.push("<ul>");
                inList = "ul";
            }
            out.push(`<li>${inline(ul[1])}</li>`);
            continue;
        }
        const ol = /^\d+\.\s+(.+)$/.exec(line.trimStart());
        if (ol) {
            flushParagraph();
            if (inList !== "ol") {
                flushList();
                out.push("<ol>");
                inList = "ol";
            }
            out.push(`<li>${inline(ol[1])}</li>`);
            continue;
        }
        flushList();
        paragraph.push(line);
    }
    flushParagraph();
    flushList();
    return out.join("\n");
}

function GeneratedDocumentPanel({
    doc,
    onDismiss,
}: {
    doc: GenerateDocumentResult;
    onDismiss: () => void;
}) {
    const [view, setView] = useState<"preview" | "source">("preview");
    const html = renderMarkdownToHtml(doc.body_markdown);

    function download() {
        const safe = doc.title.replace(/[^a-z0-9-_]+/gi, "-").toLowerCase();
        const blob = new Blob([doc.body_markdown], { type: "text/markdown" });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = `${safe || "document"}.md`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
    }
    function openPrintPreview() {
        // Open a new tab with the rendered HTML, then trigger the
        // browser print dialog. The rep can save-as-PDF from there;
        // we avoid bringing a server-side PDF library into the Python
        // image for what's already a first-class browser capability.
        const w = window.open("", "_blank", "noopener,noreferrer");
        if (!w) return;
        w.document.write(
            `<!doctype html><html><head><title>${doc.title}</title>` +
            `<style>` +
            `body{font-family:Georgia,'Times New Roman',serif;max-width:780px;margin:48px auto;padding:0 32px;color:#1a1a1a;line-height:1.55}` +
            `h1{font-size:28px;margin-bottom:16px}` +
            `h2{font-size:20px;margin-top:32px}` +
            `h3{font-size:16px;margin-top:24px;color:#444}` +
            `p{margin:12px 0}` +
            `ul,ol{margin:12px 0;padding-left:24px}` +
            `li{margin:4px 0}` +
            `</style></head><body>${html}</body></html>`,
        );
        w.document.close();
        // Let the layout settle before invoking print.
        setTimeout(() => w.print(), 250);
    }
    async function copyToClipboard() {
        try {
            await navigator.clipboard.writeText(doc.body_markdown);
        } catch {
            // ignore — older browsers without clipboard API
        }
    }

    return (
        <div className="rounded border border-fuchsia-300 bg-fuchsia-50 p-3 text-xs dark:border-fuchsia-700 dark:bg-fuchsia-900/20">
            <div className="flex flex-wrap items-baseline justify-between gap-2">
                <div className="font-semibold text-fuchsia-900 dark:text-fuchsia-100">
                    {doc.title}
                </div>
                <div className="text-[10px] text-fuchsia-700 dark:text-fuchsia-300">
                    {doc.word_count} words · {doc.model}
                </div>
            </div>
            <div className="mt-2 flex flex-wrap gap-1 text-[11px]">
                <button
                    type="button"
                    onClick={() => setView("preview")}
                    className={
                        view === "preview"
                            ? "rounded border border-fuchsia-400 bg-white px-2 py-0.5 font-medium dark:bg-fuchsia-950"
                            : "rounded border border-transparent px-2 py-0.5 text-fuchsia-700 dark:text-fuchsia-300"
                    }
                >
                    Preview
                </button>
                <button
                    type="button"
                    onClick={() => setView("source")}
                    className={
                        view === "source"
                            ? "rounded border border-fuchsia-400 bg-white px-2 py-0.5 font-medium dark:bg-fuchsia-950"
                            : "rounded border border-transparent px-2 py-0.5 text-fuchsia-700 dark:text-fuchsia-300"
                    }
                >
                    Markdown source
                </button>
                <button type="button" onClick={copyToClipboard} className="ml-auto rounded border border-fuchsia-400 px-2 py-0.5 hover:bg-fuchsia-100 dark:hover:bg-fuchsia-900/40">
                    Copy
                </button>
                <button type="button" onClick={download} className="rounded border border-fuchsia-400 px-2 py-0.5 hover:bg-fuchsia-100 dark:hover:bg-fuchsia-900/40">
                    Download .md
                </button>
                <button type="button" onClick={openPrintPreview} className="rounded border border-fuchsia-400 px-2 py-0.5 hover:bg-fuchsia-100 dark:hover:bg-fuchsia-900/40">
                    Print / save as PDF
                </button>
                <button type="button" onClick={onDismiss} className="rounded border border-fuchsia-400 px-2 py-0.5 hover:bg-fuchsia-100 dark:hover:bg-fuchsia-900/40">
                    Dismiss
                </button>
            </div>
            <div className="mt-2 max-h-96 overflow-auto rounded bg-white p-3 leading-relaxed text-slate-900 dark:bg-slate-900 dark:text-slate-100">
                {view === "preview" ? (
                    <div
                        className="prose-doc"
                        dangerouslySetInnerHTML={{ __html: html }}
                    />
                ) : (
                    <pre className="whitespace-pre-wrap text-[11px]">{doc.body_markdown}</pre>
                )}
            </div>
        </div>
    );
}

