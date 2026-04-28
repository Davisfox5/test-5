"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import {
    ScorecardCriterion,
    ScorecardTemplate,
    totalWeight,
    useCreateScorecard,
    useDeleteScorecard,
    useUpdateScorecard,
    validateScorecard,
} from "@/lib/scorecards";

const CHANNELS = ["voice", "email", "chat"] as const;

export interface ScorecardEditorProps {
    initial?: ScorecardTemplate;
    mode: "create" | "edit";
}

interface EditorState {
    name: string;
    criteria: ScorecardCriterion[];
    channel_filter: string[];
    is_default: boolean;
}

function blankCriterion(): ScorecardCriterion {
    return { name: "", weight: 0, description: "" };
}

function fromTemplate(t: ScorecardTemplate | undefined): EditorState {
    return {
        name: t?.name ?? "",
        criteria: (t?.criteria ?? []).map((c) => ({ ...c })),
        channel_filter: t?.channel_filter ?? [],
        is_default: t?.is_default ?? false,
    };
}

export function ScorecardEditor({ initial, mode }: ScorecardEditorProps) {
    const router = useRouter();
    const [state, setState] = useState<EditorState>(() => fromTemplate(initial));
    const [confirmDelete, setConfirmDelete] = useState(false);

    const create = useCreateScorecard();
    const update = useUpdateScorecard();
    const del = useDeleteScorecard();

    useEffect(() => {
        if (initial) setState(fromTemplate(initial));
    }, [initial]);

    const validation = validateScorecard(state.name, state.criteria);
    const sumWeight = totalWeight(state.criteria);

    function patchCriterion(idx: number, patch: Partial<ScorecardCriterion>) {
        setState((s) => ({
            ...s,
            criteria: s.criteria.map((c, i) => (i === idx ? { ...c, ...patch } : c)),
        }));
    }

    function addCriterion() {
        setState((s) => ({ ...s, criteria: [...s.criteria, blankCriterion()] }));
    }

    function removeCriterion(idx: number) {
        setState((s) => ({
            ...s,
            criteria: s.criteria.filter((_, i) => i !== idx),
        }));
    }

    function toggleChannel(ch: string) {
        setState((s) => ({
            ...s,
            channel_filter: s.channel_filter.includes(ch)
                ? s.channel_filter.filter((c) => c !== ch)
                : [...s.channel_filter, ch],
        }));
    }

    async function handleSave() {
        if (!validation.ok) return;
        const payload = {
            name: state.name.trim(),
            criteria: state.criteria.map((c) => ({
                ...c,
                name: c.name.trim(),
                weight: Number(c.weight),
                description: (c.description ?? "").trim(),
            })),
            channel_filter:
                state.channel_filter.length > 0 ? state.channel_filter : null,
            is_default: state.is_default,
        };
        if (mode === "create") {
            const created = await create.mutateAsync(payload);
            router.push(`/scorecards/${created.id}`);
        } else if (initial) {
            await update.mutateAsync({ id: initial.id, patch: payload });
        }
    }

    async function handleDelete() {
        if (!initial) return;
        await del.mutateAsync(initial.id);
        router.push("/scorecards");
    }

    const saving = create.isPending || update.isPending;
    const saveError = create.error ?? update.error;

    return (
        <div className="space-y-6">
            <header className="flex items-start justify-between gap-4 flex-wrap">
                <div className="flex-1 min-w-0 space-y-1">
                    <Link
                        href="/scorecards"
                        className="text-sm text-text-subtle hover:text-text-muted"
                    >
                        ← Back to scorecards
                    </Link>
                    <input
                        type="text"
                        value={state.name}
                        onChange={(e) =>
                            setState((s) => ({ ...s, name: e.target.value }))
                        }
                        placeholder="Scorecard name"
                        className="block w-full bg-transparent text-2xl font-bold focus:outline-none"
                    />
                </div>
                <div className="flex gap-2">
                    {mode === "edit" ? (
                        <button
                            onClick={() => setConfirmDelete(true)}
                            disabled={del.isPending}
                            className="rounded-md border border-border px-3 py-1.5 text-sm text-accent-rose hover:bg-bg-secondary disabled:opacity-50"
                        >
                            Delete
                        </button>
                    ) : null}
                    <button
                        onClick={handleSave}
                        disabled={!validation.ok || saving}
                        className="rounded-md bg-primary text-white px-3 py-1.5 text-sm font-medium hover:bg-primary/90 disabled:opacity-50"
                    >
                        {saving ? "Saving…" : "Save"}
                    </button>
                </div>
            </header>

            <Section
                title="Rubric items"
                subtitle="Each item gets graded individually. Weights must sum to 100."
            >
                {state.criteria.length === 0 ? (
                    <p className="text-sm text-text-subtle py-4 text-center">
                        No rubric items yet.
                    </p>
                ) : (
                    <div className="space-y-3">
                        {state.criteria.map((c, idx) => (
                            <div
                                key={idx}
                                className="rounded-md border border-border bg-bg-elevated p-3 space-y-2"
                            >
                                <div className="flex gap-2">
                                    <input
                                        type="text"
                                        value={c.name}
                                        onChange={(e) =>
                                            patchCriterion(idx, {
                                                name: e.target.value,
                                            })
                                        }
                                        placeholder="Criterion name (e.g. Greeting)"
                                        className="flex-1 rounded-md border border-border bg-bg-card px-3 py-1.5 text-sm"
                                    />
                                    <label className="flex items-center gap-1 text-xs text-text-muted">
                                        Weight
                                        <input
                                            type="number"
                                            min={0}
                                            max={100}
                                            value={c.weight}
                                            onChange={(e) =>
                                                patchCriterion(idx, {
                                                    weight: Number(e.target.value),
                                                })
                                            }
                                            className="w-20 rounded-md border border-border bg-bg-card px-2 py-1 text-sm"
                                        />
                                    </label>
                                    <button
                                        onClick={() => removeCriterion(idx)}
                                        className="rounded-md border border-border px-2 py-1 text-xs text-accent-rose hover:bg-bg-secondary"
                                        aria-label="Remove rubric item"
                                    >
                                        ✕
                                    </button>
                                </div>
                                <textarea
                                    value={c.description ?? ""}
                                    onChange={(e) =>
                                        patchCriterion(idx, {
                                            description: e.target.value,
                                        })
                                    }
                                    placeholder="What should the grader look for?"
                                    rows={2}
                                    className="w-full rounded-md border border-border bg-bg-card px-3 py-1.5 text-sm"
                                />
                            </div>
                        ))}
                    </div>
                )}

                <div className="flex items-center justify-between mt-4">
                    <button
                        onClick={addCriterion}
                        className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-bg-secondary"
                    >
                        + Add item
                    </button>
                    <p
                        className={`text-sm ${
                            sumWeight === 100
                                ? "text-accent-emerald"
                                : "text-accent-amber"
                        }`}
                    >
                        Total weight: {sumWeight} / 100
                    </p>
                </div>
            </Section>

            <Section
                title="Channels"
                subtitle="Limit which interaction channels Linda grades against this rubric. Leave empty for all."
            >
                <div className="flex flex-wrap gap-2">
                    {CHANNELS.map((ch) => {
                        const on = state.channel_filter.includes(ch);
                        return (
                            <button
                                key={ch}
                                onClick={() => toggleChannel(ch)}
                                className={`rounded-md border px-3 py-1.5 text-sm capitalize ${
                                    on
                                        ? "border-primary bg-primary-soft text-primary"
                                        : "border-border hover:bg-bg-secondary"
                                }`}
                            >
                                {ch}
                            </button>
                        );
                    })}
                </div>
            </Section>

            <Section title="Defaults">
                <label className="flex items-start gap-3 text-sm">
                    <input
                        type="checkbox"
                        checked={state.is_default}
                        onChange={(e) =>
                            setState((s) => ({
                                ...s,
                                is_default: e.target.checked,
                            }))
                        }
                        className="mt-1 h-4 w-4"
                    />
                    <span>
                        <div className="font-medium">Use as default scorecard</div>
                        <div className="text-text-muted">
                            New calls will be graded with this template unless a
                            channel-specific match exists.
                        </div>
                    </span>
                </label>
            </Section>

            {!validation.ok ? (
                <div className="rounded-lg border border-accent-amber bg-bg-card p-3 text-sm text-accent-amber space-y-1">
                    {validation.errors.map((e) => (
                        <div key={e}>{e}</div>
                    ))}
                </div>
            ) : null}

            {saveError ? (
                <div className="rounded-lg border border-accent-rose bg-bg-card p-3 text-sm text-accent-rose">
                    Save failed: {(saveError as Error).message}
                </div>
            ) : null}

            {confirmDelete && initial ? (
                <ConfirmDialog
                    title={`Delete "${initial.name}"?`}
                    body="This is permanent. Existing scores stay; future calls won't be graded against this template."
                    confirmLabel={del.isPending ? "Deleting…" : "Delete"}
                    onCancel={() => setConfirmDelete(false)}
                    onConfirm={handleDelete}
                />
            ) : null}
        </div>
    );
}

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

function ConfirmDialog({
    title,
    body,
    onCancel,
    onConfirm,
    confirmLabel,
}: {
    title: string;
    body: string;
    onCancel: () => void;
    onConfirm: () => void;
    confirmLabel: string;
}) {
    return (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
            <div className="w-full max-w-md rounded-lg border border-border bg-bg-card p-5 shadow-lg">
                <h3 className="text-lg font-semibold">{title}</h3>
                <p className="mt-2 text-sm text-text-muted">{body}</p>
                <div className="mt-5 flex justify-end gap-2">
                    <button
                        onClick={onCancel}
                        className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-bg-secondary"
                    >
                        Cancel
                    </button>
                    <button
                        onClick={onConfirm}
                        className="rounded-md bg-accent-rose text-white px-3 py-1.5 text-sm hover:opacity-90"
                    >
                        {confirmLabel}
                    </button>
                </div>
            </div>
        </div>
    );
}
