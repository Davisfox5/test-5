"use client";

import { useState } from "react";

import { ActionPlan, ActionStep, useAddNote } from "@/lib/action-plans";

interface NoteInputProps {
    plan: ActionPlan;
    step: ActionStep;
}

/**
 * Add-note affordance on each step card.
 *
 * Behavior:
 * - Agent types a freeform note ("Vendor called instead of emailing;
 *   tier limits are 50k/100k/250k").
 * - On save, the API runs Call D against the note text using the
 *   step's output_schema. Extracted slot values auto-apply per the
 *   locked decision.
 * - Cancel discards the draft.
 */
export function NoteInput({ plan, step }: NoteInputProps) {
    const [open, setOpen] = useState(false);
    const [text, setText] = useState("");
    const m = useAddNote(plan.id);

    const submit = () => {
        const trimmed = text.trim();
        if (!trimmed) return;
        m.mutate(
            { stepId: step.id, note_text: trimmed },
            {
                onSuccess: () => {
                    setText("");
                    setOpen(false);
                },
            },
        );
    };

    if (!open) {
        return (
            <button
                type="button"
                className="text-xs text-indigo-600 hover:underline dark:text-indigo-400"
                onClick={() => setOpen(true)}
            >
                + Add a note
            </button>
        );
    }

    return (
        <div className="rounded border border-slate-200 p-2 dark:border-slate-700">
            <textarea
                className="block w-full rounded border border-slate-300 p-1 text-xs dark:border-slate-600 dark:bg-slate-900"
                rows={3}
                placeholder="e.g. Vendor called instead of emailing. Tier limits are 50k / 100k / 250k seats."
                value={text}
                onChange={(e) => setText(e.target.value)}
                disabled={m.isPending}
            />
            <div className="mt-2 flex justify-end gap-2">
                <button
                    type="button"
                    className="rounded border border-slate-300 px-2 py-1 text-xs dark:border-slate-600"
                    onClick={() => {
                        setText("");
                        setOpen(false);
                    }}
                    disabled={m.isPending}
                >
                    Cancel
                </button>
                <button
                    type="button"
                    className="rounded bg-indigo-600 px-2 py-1 text-xs text-white disabled:opacity-60"
                    onClick={submit}
                    disabled={m.isPending || !text.trim()}
                >
                    {m.isPending ? "Saving…" : "Save & extract"}
                </button>
            </div>
            {m.isError && (
                <p className="mt-1 text-xs text-rose-600 dark:text-rose-400">
                    Save failed. Try again.
                </p>
            )}
        </div>
    );
}
