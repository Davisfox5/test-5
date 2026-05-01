"use client";

/**
 * Retention overrides — admin sub-section under /settings.
 *
 * Two number inputs (audio hours, feedback days). Empty / cleared input
 * sends ``null`` to PATCH /admin/tenant-settings, which clears the
 * override and falls back to the platform default. The nightly
 * event_retention sweep picks up the new threshold within 24h.
 */

import { useEffect, useState } from "react";
import {
    useTenantSettings,
    useUpdateTenantSettings,
} from "@/lib/tenant-settings";
import { humanizeError } from "@/components/admin/section";

export function RetentionSection() {
    const { data, isLoading, error } = useTenantSettings();
    const update = useUpdateTenantSettings();

    // Track the input as a string so the user can clear it. Empty string
    // means "clear override" → we PATCH null on save.
    const [audioHours, setAudioHours] = useState<string>("");
    const [feedbackDays, setFeedbackDays] = useState<string>("");
    const [localError, setLocalError] = useState<string | null>(null);

    // Hydrate the inputs whenever the server payload arrives. We seed
    // audio with the current value (since the column is non-nullable),
    // and feedback with the override (or empty if null).
    useEffect(() => {
        if (!data) return;
        setAudioHours(
            data.audio_retention_hours === data.audio_retention_hours_default
                ? ""
                : String(data.audio_retention_hours),
        );
        setFeedbackDays(
            data.feedback_retention_days_override == null
                ? ""
                : String(data.feedback_retention_days_override),
        );
    }, [data]);

    if (isLoading) {
        return <p className="text-sm text-text-muted">Loading retention…</p>;
    }
    if (error) {
        return (
            <p className="text-sm text-accent-rose">
                Couldn't load retention settings: {humanizeError(error)}
            </p>
        );
    }
    if (!data) return null;

    const audioDefault = data.audio_retention_hours_default;
    const feedbackDefault = data.feedback_retention_days_default;

    const parseOptional = (
        raw: string,
        min: number,
        max: number,
        label: string,
    ): { ok: true; value: number | null } | { ok: false; error: string } => {
        const trimmed = raw.trim();
        if (!trimmed) return { ok: true, value: null };
        const n = Number(trimmed);
        if (!Number.isFinite(n) || !Number.isInteger(n)) {
            return { ok: false, error: `${label} must be a whole number.` };
        }
        if (n < min || n > max) {
            return {
                ok: false,
                error: `${label} must be between ${min} and ${max}.`,
            };
        }
        return { ok: true, value: n };
    };

    const onSave = () => {
        setLocalError(null);
        const audio = parseOptional(audioHours, 1, 8760, "Audio retention hours");
        if (!audio.ok) {
            setLocalError(audio.error);
            return;
        }
        const feedback = parseOptional(
            feedbackDays,
            1,
            3650,
            "Feedback retention days",
        );
        if (!feedback.ok) {
            setLocalError(feedback.error);
            return;
        }
        update.mutate({
            audio_retention_hours_override: audio.value,
            feedback_retention_days_override: feedback.value,
        });
    };

    return (
        <div className="space-y-4">
            <p className="text-sm text-text-muted">
                Lower retention saves storage and reduces compliance scope;
                higher retention preserves more historical training data for
                AI tuning. Changes apply at the next nightly retention sweep
                — existing data older than your new threshold is purged
                within 24 hours.
            </p>

            <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
                <div>
                    <label
                        htmlFor="retention-audio-hours"
                        className="text-sm font-medium"
                    >
                        Audio recording retention (hours)
                    </label>
                    <p className="text-xs text-text-subtle mb-1">
                        Platform default: {audioDefault}h. Leave blank to use
                        the default.
                    </p>
                    <input
                        id="retention-audio-hours"
                        type="number"
                        min={1}
                        max={8760}
                        step={1}
                        inputMode="numeric"
                        placeholder={String(audioDefault)}
                        className="w-full rounded-md border border-border bg-bg-raised px-3 py-2 text-sm"
                        value={audioHours}
                        onChange={(e) => setAudioHours(e.target.value)}
                        disabled={update.isPending}
                    />
                </div>

                <div>
                    <label
                        htmlFor="retention-feedback-days"
                        className="text-sm font-medium"
                    >
                        Feedback event retention (days)
                    </label>
                    <p className="text-xs text-text-subtle mb-1">
                        Platform default: {feedbackDefault}d. Leave blank to
                        use the default.
                    </p>
                    <input
                        id="retention-feedback-days"
                        type="number"
                        min={1}
                        max={3650}
                        step={1}
                        inputMode="numeric"
                        placeholder={String(feedbackDefault)}
                        className="w-full rounded-md border border-border bg-bg-raised px-3 py-2 text-sm"
                        value={feedbackDays}
                        onChange={(e) => setFeedbackDays(e.target.value)}
                        disabled={update.isPending}
                    />
                </div>
            </div>

            <div className="flex items-center gap-3">
                <button
                    type="button"
                    onClick={onSave}
                    disabled={update.isPending}
                    className="rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-white disabled:opacity-50"
                >
                    {update.isPending ? "Saving…" : "Save retention"}
                </button>
                {localError ? (
                    <span className="text-xs text-accent-rose">{localError}</span>
                ) : null}
                {update.isError ? (
                    <span className="text-xs text-accent-rose">
                        {humanizeError(update.error)}
                    </span>
                ) : null}
                {update.isSuccess && !localError ? (
                    <span className="text-xs text-accent-emerald">Saved.</span>
                ) : null}
            </div>
        </div>
    );
}
