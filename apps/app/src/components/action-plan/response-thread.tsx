"use client";

import { ActionStep, StepResponse } from "@/lib/action-plans";

interface ResponseThreadProps {
    step: ActionStep;
}

function sourceLabel(source: string): string {
    switch (source) {
        case "inbound_email":
            return "Inbound reply";
        case "manual_note":
            return "Agent note";
        case "outbound_email_sent":
            return "Outbound email sent";
        case "auto_mark_done":
            return "Marked done";
        default:
            return source;
    }
}

function formatTime(iso: string): string {
    try {
        return new Date(iso).toLocaleString();
    } catch {
        return iso;
    }
}

function ResponseRow({ r }: { r: StepResponse }) {
    const filled = Object.keys(r.extracted_data || {});
    const unfilled = Object.keys(r.unfilled_reasons || {});
    return (
        <li className="rounded border border-slate-200 p-2 text-xs dark:border-slate-700">
            <div className="flex items-center justify-between">
                <span className="font-medium">{sourceLabel(r.source)}</span>
                <span className="text-[10px] text-slate-500">{formatTime(r.received_at)}</span>
            </div>
            {r.note_text ? (
                <p className="mt-1 whitespace-pre-wrap text-[11px] text-slate-700 dark:text-slate-300">
                    {r.note_text}
                </p>
            ) : null}
            {filled.length > 0 ? (
                <div className="mt-2 flex flex-wrap gap-1">
                    {filled.map((slot) => (
                        <span
                            key={slot}
                            className="rounded bg-emerald-100 px-1.5 py-0.5 font-mono text-[10px] text-emerald-900 dark:bg-emerald-900/40 dark:text-emerald-100"
                            title={r.source_quotes?.[slot] ?? ""}
                        >
                            {slot}: {String(r.extracted_data[slot])}
                        </span>
                    ))}
                </div>
            ) : null}
            {unfilled.length > 0 ? (
                <p className="mt-1 text-[10px] italic text-slate-500">
                    Could not extract: {unfilled.join(", ")}
                </p>
            ) : null}
            {r.agent_overridden && (
                <p className="mt-1 text-[10px] italic text-amber-600 dark:text-amber-300">
                    Agent overrode an extracted value.
                </p>
            )}
        </li>
    );
}

export function ResponseThread({ step }: ResponseThreadProps) {
    if (!step.responses || step.responses.length === 0) return null;
    return (
        <div>
            <h4 className="text-[11px] font-semibold uppercase tracking-wide text-slate-500">
                Responses
            </h4>
            <ul className="mt-1 space-y-1">
                {step.responses.map((r) => (
                    <ResponseRow key={r.id} r={r} />
                ))}
            </ul>
        </div>
    );
}
