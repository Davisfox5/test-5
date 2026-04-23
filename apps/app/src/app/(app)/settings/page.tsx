"use client";

import { useMe } from "@/lib/me";
import {
    FeatureFlagSpec,
    useTenantSettings,
    useUpdateTenantSettings,
} from "@/lib/tenant-settings";

export default function SettingsPage() {
    const { data: me } = useMe();
    const { data: settings, isLoading, error } = useTenantSettings();
    const update = useUpdateTenantSettings();

    const isExec = me?.user.role === "executive";

    return (
        <div className="space-y-6">
            <header>
                <h2 className="text-2xl font-bold">Settings</h2>
                <p className="text-text-muted mt-1">
                    Workspace preferences for {me?.tenant.name ?? "your tenant"}.
                </p>
            </header>

            {error ? (
                <ErrorCard message={humanizeError(error)} />
            ) : null}

            {isLoading ? (
                <SkeletonCard />
            ) : settings ? (
                <>
                    <Section
                        title="Feature flags"
                        subtitle="Toggle which LINDA features are available for your team."
                    >
                        <div className="space-y-2">
                            {settings.feature_flag_spec.map((spec) => (
                                <FlagRow
                                    key={spec.key}
                                    spec={spec}
                                    value={
                                        settings.features_enabled[spec.key] ??
                                        spec.default
                                    }
                                    disabled={!isExec || update.isPending}
                                    onChange={(next) =>
                                        update.mutate({
                                            features_enabled: { [spec.key]: next },
                                        })
                                    }
                                />
                            ))}
                        </div>
                        {!isExec ? (
                            <p className="mt-4 text-xs text-text-subtle">
                                Contact an executive on your team to change
                                these flags.
                            </p>
                        ) : null}
                    </Section>

                    <Section
                        title="Transcription"
                        subtitle="How LINDA turns call audio into analyzable transcripts."
                    >
                        <RadioGroup
                            label="Engine"
                            value={settings.transcription_engine}
                            disabled={!isExec || update.isPending}
                            options={[
                                {
                                    value: "deepgram",
                                    label: "Deepgram Nova-3",
                                    help: "Cloud ASR with native diarization (recommended).",
                                },
                                {
                                    value: "whisper",
                                    label: "Self-hosted Whisper",
                                    help: "Runs on your infra — slower, higher CPU, no cloud egress.",
                                },
                            ]}
                            onChange={(next) =>
                                update.mutate({
                                    transcription_engine:
                                        next as "deepgram" | "whisper",
                                })
                            }
                        />
                    </Section>

                    <Section
                        title="Automation"
                        subtitle="How far LINDA will act on its own vs. queue for review."
                    >
                        <RadioGroup
                            label="Automation level"
                            value={settings.automation_level}
                            disabled={!isExec || update.isPending}
                            options={[
                                {
                                    value: "approval",
                                    label: "Require approval",
                                    help: "Every action needs a human sign-off.",
                                },
                                {
                                    value: "auto",
                                    label: "Auto-apply",
                                    help: "High-confidence actions fire without review.",
                                },
                                {
                                    value: "shadow",
                                    label: "Shadow mode",
                                    help: "LINDA drafts everything but never sends.",
                                },
                            ]}
                            onChange={(next) =>
                                update.mutate({
                                    automation_level: next as
                                        | "approval"
                                        | "auto"
                                        | "shadow",
                                })
                            }
                        />
                    </Section>

                    <Section
                        title="Privacy"
                        subtitle="Data-handling defaults for this workspace."
                    >
                        <FlagRow
                            spec={{
                                key: "pii_redaction_enabled",
                                default: true,
                                label: "PII redaction on transcripts",
                                help: "Mask emails, phone numbers, and other direct identifiers before they land in insights.",
                            }}
                            value={settings.pii_redaction_enabled}
                            disabled={!isExec || update.isPending}
                            onChange={(next) =>
                                update.mutate({ pii_redaction_enabled: next })
                            }
                        />
                    </Section>

                    {update.isError ? (
                        <ErrorCard
                            message={`Couldn’t save: ${humanizeError(
                                update.error,
                            )}`}
                        />
                    ) : null}
                </>
            ) : null}
        </div>
    );
}

/* ── Subcomponents ────────────────────────────────────────────────── */

function Section({
    title,
    subtitle,
    children,
}: {
    title: string;
    subtitle?: string;
    children: React.ReactNode;
}) {
    return (
        <section className="rounded-lg border border-border bg-bg-card p-6">
            <div className="mb-4">
                <h3 className="text-lg font-semibold">{title}</h3>
                {subtitle ? (
                    <p className="text-sm text-text-muted mt-1">{subtitle}</p>
                ) : null}
            </div>
            {children}
        </section>
    );
}

function FlagRow({
    spec,
    value,
    disabled,
    onChange,
}: {
    spec: FeatureFlagSpec;
    value: boolean;
    disabled?: boolean;
    onChange: (next: boolean) => void;
}) {
    return (
        <label className="flex items-start justify-between gap-4 py-2 border-b border-border last:border-b-0">
            <div className="flex-1 min-w-0">
                <div className="font-medium">{spec.label}</div>
                <div className="text-sm text-text-muted mt-0.5">
                    {spec.help}
                </div>
            </div>
            <input
                type="checkbox"
                className="mt-1 h-5 w-9 cursor-pointer accent-accent"
                checked={value}
                disabled={disabled}
                onChange={(e) => onChange(e.target.checked)}
                aria-label={spec.label}
            />
        </label>
    );
}

function RadioGroup<T extends string>({
    label,
    value,
    options,
    disabled,
    onChange,
}: {
    label: string;
    value: T;
    options: { value: T; label: string; help?: string }[];
    disabled?: boolean;
    onChange: (next: T) => void;
}) {
    return (
        <fieldset>
            <legend className="text-sm font-medium mb-3">{label}</legend>
            <div className="space-y-2">
                {options.map((opt) => (
                    <label
                        key={opt.value}
                        className={`flex items-start gap-3 rounded-md border border-border p-3 ${
                            value === opt.value
                                ? "bg-bg-raised"
                                : "hover:bg-bg-raised"
                        } ${disabled ? "opacity-50" : "cursor-pointer"}`}
                    >
                        <input
                            type="radio"
                            className="mt-1"
                            name={label}
                            value={opt.value}
                            checked={value === opt.value}
                            disabled={disabled}
                            onChange={() => onChange(opt.value)}
                        />
                        <div className="flex-1 min-w-0">
                            <div className="font-medium">{opt.label}</div>
                            {opt.help ? (
                                <div className="text-xs text-text-muted mt-0.5">
                                    {opt.help}
                                </div>
                            ) : null}
                        </div>
                    </label>
                ))}
            </div>
        </fieldset>
    );
}

function SkeletonCard() {
    return (
        <div className="rounded-lg border border-border bg-bg-card p-6 animate-pulse">
            <div className="h-4 w-40 bg-bg-raised rounded mb-3" />
            <div className="h-3 w-64 bg-bg-raised rounded" />
        </div>
    );
}

function ErrorCard({ message }: { message: string }) {
    return (
        <div className="rounded-lg border border-accent-alert bg-accent-alert-subtle text-accent-alert-strong p-4 text-sm">
            {message}
        </div>
    );
}

function humanizeError(error: unknown): string {
    if (error instanceof Error) return error.message;
    return "Unexpected error — see console for details.";
}
