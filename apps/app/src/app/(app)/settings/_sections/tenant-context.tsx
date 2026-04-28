"use client";

import { useEffect, useState } from "react";
import {
    useRebuildTenantContext,
    useTenantContext,
    useUpdateTenantContextFields,
} from "@/lib/tenant-context";
import { humanizeError } from "@/components/admin/section";

type StringListKey = "goals" | "strategies";

export function TenantContextSection() {
    const { data, isLoading, error } = useTenantContext();
    const update = useUpdateTenantContextFields();
    const rebuild = useRebuildTenantContext();

    const [draftJson, setDraftJson] = useState<string>("");
    const [parseError, setParseError] = useState<string | null>(null);

    // Reset the editor whenever the server brief changes (initial load,
    // rebuild completion, etc) so admins don't keep editing stale JSON.
    useEffect(() => {
        if (data?.brief) {
            setDraftJson(JSON.stringify(data.brief, null, 2));
        }
    }, [data?.brief]);

    if (isLoading) {
        return <p className="text-sm text-text-muted">Loading brief…</p>;
    }
    if (error) {
        return (
            <p className="text-sm text-accent-rose">
                Couldn't load tenant context: {humanizeError(error)}
            </p>
        );
    }
    if (!data) return null;

    const brief = data.brief ?? {};
    const goals = (brief["goals"] as string[] | undefined) ?? [];
    const strategies = (brief["strategies"] as string[] | undefined) ?? [];

    const onSaveJson = () => {
        setParseError(null);
        let parsed: unknown;
        try {
            parsed = JSON.parse(draftJson || "{}");
        } catch (err) {
            setParseError(humanizeError(err));
            return;
        }
        if (typeof parsed !== "object" || parsed === null) {
            setParseError("Brief must be a JSON object.");
            return;
        }
        update.mutate(parsed as Record<string, unknown>);
    };

    return (
        <div className="space-y-4">
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
                <ListField
                    label="Goals"
                    keyName="goals"
                    items={goals}
                    onChange={(next) => update.mutate({ goals: next })}
                    disabled={update.isPending}
                />
                <ListField
                    label="Strategies"
                    keyName="strategies"
                    items={strategies}
                    onChange={(next) => update.mutate({ strategies: next })}
                    disabled={update.isPending}
                />
            </div>

            <div>
                <label className="text-sm font-medium" htmlFor="brief-json">
                    Full brief (JSON)
                </label>
                <p className="text-xs text-text-subtle mb-1">
                    Direct edit for industry, ICPs, products, KPIs, and any
                    custom keys. Validated as JSON before saving.
                </p>
                <textarea
                    id="brief-json"
                    className="mt-1 w-full min-h-[180px] rounded-md border border-border bg-bg-raised p-3 font-mono text-xs"
                    value={draftJson}
                    onChange={(e) => setDraftJson(e.target.value)}
                    spellCheck={false}
                />
                <div className="mt-2 flex items-center gap-2">
                    <button
                        type="button"
                        onClick={onSaveJson}
                        disabled={update.isPending}
                        className="rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-white disabled:opacity-50"
                    >
                        {update.isPending ? "Saving…" : "Save brief"}
                    </button>
                    <button
                        type="button"
                        onClick={() => rebuild.mutate()}
                        disabled={rebuild.isPending}
                        className="rounded-md border border-border bg-bg-raised px-3 py-1.5 text-xs font-medium hover:bg-bg-card disabled:opacity-50"
                    >
                        {rebuild.isPending
                            ? "Re-inferring…"
                            : "Re-infer from sources"}
                    </button>
                    {parseError ? (
                        <span className="text-xs text-accent-rose">
                            {parseError}
                        </span>
                    ) : null}
                    {update.isError ? (
                        <span className="text-xs text-accent-rose">
                            {humanizeError(update.error)}
                        </span>
                    ) : null}
                    {rebuild.isSuccess ? (
                        <span className="text-xs text-accent-emerald">
                            Rebuild scheduled.
                        </span>
                    ) : null}
                </div>
            </div>

            {data.prompt_preview ? (
                <details className="rounded-md border border-border bg-bg-raised p-3">
                    <summary className="cursor-pointer text-xs font-medium text-text-muted">
                        Prompt preview
                    </summary>
                    <pre className="mt-2 whitespace-pre-wrap text-xs text-text-subtle">
                        {data.prompt_preview}
                    </pre>
                </details>
            ) : null}
        </div>
    );
}

function ListField({
    label,
    keyName,
    items,
    onChange,
    disabled,
}: {
    label: string;
    keyName: StringListKey;
    items: string[];
    onChange: (next: string[]) => void;
    disabled?: boolean;
}) {
    const [draft, setDraft] = useState("");
    return (
        <div>
            <label className="text-sm font-medium" htmlFor={`tc-${keyName}`}>
                {label}
            </label>
            <ul className="mt-1 space-y-1">
                {items.map((item, idx) => (
                    <li
                        key={`${keyName}-${idx}`}
                        className="flex items-center gap-2 rounded border border-border bg-bg-raised px-2 py-1 text-xs"
                    >
                        <span className="flex-1 truncate">{item}</span>
                        <button
                            type="button"
                            disabled={disabled}
                            onClick={() =>
                                onChange(items.filter((_, i) => i !== idx))
                            }
                            className="text-text-subtle hover:text-accent-rose"
                            aria-label={`Remove ${label} ${idx + 1}`}
                        >
                            ×
                        </button>
                    </li>
                ))}
            </ul>
            <div className="mt-1 flex items-center gap-2">
                <input
                    id={`tc-${keyName}`}
                    type="text"
                    className="flex-1 rounded-md border border-border bg-bg-raised px-2 py-1 text-xs"
                    placeholder={`Add ${label.toLowerCase()}…`}
                    value={draft}
                    disabled={disabled}
                    onChange={(e) => setDraft(e.target.value)}
                    onKeyDown={(e) => {
                        if (e.key === "Enter" && draft.trim()) {
                            e.preventDefault();
                            onChange([...items, draft.trim()]);
                            setDraft("");
                        }
                    }}
                />
                <button
                    type="button"
                    disabled={disabled || !draft.trim()}
                    onClick={() => {
                        onChange([...items, draft.trim()]);
                        setDraft("");
                    }}
                    className="rounded-md border border-border bg-bg-raised px-2 py-1 text-xs disabled:opacity-50"
                >
                    Add
                </button>
            </div>
        </div>
    );
}
