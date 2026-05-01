"use client";

import { useMemo, useState } from "react";
import {
    ApiKey,
    ApiKeyCreated,
    useApiKeys,
    useCreateApiKey,
    useRevokeApiKey,
    useScopeCatalog,
    useUpdateApiKey,
} from "@/lib/api-keys";
import { Modal } from "@/components/admin/modal";
import { humanizeError } from "@/components/admin/section";

/**
 * Settings → API keys.
 *
 * Now backed by enforced scopes. Each key carries an explicit list of
 * canonical scopes (see ``backend/app/auth.py::API_KEY_SCOPES``); the
 * create form defaults to the read-only subset the backend reports. The
 * "All scopes" preset wires up the legacy ``["*"]`` opt-in behind a
 * confirmation modal so it can't be selected by accident.
 */
export function ApiKeysSection() {
    const { data: keys, isLoading, error } = useApiKeys();
    const { data: catalog } = useScopeCatalog();
    const create = useCreateApiKey();
    const revoke = useRevokeApiKey();
    const update = useUpdateApiKey();

    const allScopes = catalog?.scopes ?? [];
    const defaultReadOnly = useMemo(
        () => new Set(catalog?.default_read_only ?? []),
        [catalog?.default_read_only],
    );

    const [showCreate, setShowCreate] = useState(false);
    const [showAllConfirm, setShowAllConfirm] = useState(false);
    const [name, setName] = useState("");
    const [pickedScopes, setPickedScopes] = useState<Set<string>>(new Set());
    const [created, setCreated] = useState<ApiKeyCreated | null>(null);
    const [copied, setCopied] = useState(false);
    const [editing, setEditing] = useState<ApiKey | null>(null);
    const [editScopes, setEditScopes] = useState<Set<string>>(new Set());

    /** Reset the create-form local state to "default read-only checked". */
    const resetCreateForm = () => {
        setName("");
        // Pre-check the read-only default set so admins can leave it as-is
        // for a low-privilege key without ticking N boxes.
        setPickedScopes(new Set(defaultReadOnly));
    };

    const onSubmit = async (e: React.FormEvent) => {
        e.preventDefault();
        try {
            const out = await create.mutateAsync({
                name: name || undefined,
                scopes: Array.from(pickedScopes),
            });
            setCreated(out);
            setName("");
        } catch {
            // surface own error below
        }
    };

    const closeAll = () => {
        setShowCreate(false);
        setCreated(null);
        setCopied(false);
        setShowAllConfirm(false);
    };

    const togglePicked = (scope: string) => {
        setPickedScopes((prev) => {
            const next = new Set(prev);
            if (next.has(scope)) next.delete(scope);
            else next.add(scope);
            return next;
        });
    };

    const onAllScopes = () => setShowAllConfirm(true);
    const confirmAllScopes = () => {
        setPickedScopes(new Set(["*"]));
        setShowAllConfirm(false);
    };

    const startEdit = (k: ApiKey) => {
        setEditing(k);
        setEditScopes(new Set(k.scopes ?? []));
    };

    const saveEdit = async () => {
        if (!editing) return;
        try {
            await update.mutateAsync({
                id: editing.id,
                payload: { scopes: Array.from(editScopes) },
            });
            setEditing(null);
        } catch {
            // fall through to error display
        }
    };

    return (
        <div className="space-y-3">
            <div className="flex justify-end">
                <button
                    type="button"
                    onClick={() => {
                        resetCreateForm();
                        setShowCreate(true);
                    }}
                    className="rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-white"
                >
                    New API key
                </button>
            </div>

            {isLoading ? (
                <p className="text-sm text-text-muted">Loading keys…</p>
            ) : error ? (
                <p className="text-sm text-accent-rose">
                    {humanizeError(error)}
                </p>
            ) : !keys?.length ? (
                <p className="text-sm text-text-muted">
                    No API keys yet. Create one above to start integrating.
                </p>
            ) : (
                <table className="w-full text-sm">
                    <thead>
                        <tr className="text-left text-xs uppercase tracking-wide text-text-subtle">
                            <th className="pb-2">Name</th>
                            <th className="pb-2">Scopes</th>
                            <th className="pb-2">Last used</th>
                            <th className="pb-2">Created</th>
                            <th className="pb-2 sr-only">Actions</th>
                        </tr>
                    </thead>
                    <tbody>
                        {keys.map((k) => (
                            <tr
                                key={k.id}
                                className="border-t border-border align-middle"
                            >
                                <td className="py-2">
                                    {k.name ?? <em>unnamed</em>}
                                </td>
                                <td className="py-2 text-xs text-text-subtle">
                                    {k.scopes && k.scopes.length > 0 ? (
                                        k.scopes.includes("*") ? (
                                            <span className="rounded bg-accent-amber/15 px-1.5 py-0.5 text-accent-amber">
                                                all scopes
                                            </span>
                                        ) : (
                                            <span title={k.scopes.join(", ")}>
                                                {k.scopes.length === 1
                                                    ? k.scopes[0]
                                                    : `${k.scopes.length} scopes`}
                                            </span>
                                        )
                                    ) : (
                                        <em>none</em>
                                    )}
                                </td>
                                <td className="py-2 text-xs text-text-subtle">
                                    {k.last_used_at
                                        ? new Date(
                                              k.last_used_at,
                                          ).toLocaleString()
                                        : "Never"}
                                </td>
                                <td className="py-2 text-xs text-text-subtle">
                                    {new Date(k.created_at).toLocaleDateString()}
                                </td>
                                <td className="py-2 text-right space-x-2">
                                    <button
                                        type="button"
                                        onClick={() => startEdit(k)}
                                        className="text-xs text-primary hover:underline"
                                    >
                                        Edit
                                    </button>
                                    <button
                                        type="button"
                                        onClick={() => {
                                            if (
                                                confirm(
                                                    `Revoke key "${k.name ?? k.id}"? This cannot be undone.`,
                                                )
                                            )
                                                revoke.mutate(k.id);
                                        }}
                                        className="text-xs text-accent-rose hover:underline"
                                    >
                                        Revoke
                                    </button>
                                </td>
                            </tr>
                        ))}
                    </tbody>
                </table>
            )}

            <Modal
                open={showCreate}
                onClose={closeAll}
                title={created ? "Save your new API key" : "Create API key"}
            >
                {created ? (
                    <div className="space-y-3">
                        <p className="text-sm text-text-muted">
                            Copy this key now — it will not be shown again.
                        </p>
                        <div className="flex items-center gap-2">
                            <code className="flex-1 truncate rounded-md border border-border bg-bg-raised px-3 py-2 font-mono text-xs">
                                {created.key}
                            </code>
                            <button
                                type="button"
                                onClick={() => {
                                    navigator.clipboard.writeText(created.key);
                                    setCopied(true);
                                }}
                                className="rounded-md bg-primary px-3 py-2 text-xs font-medium text-white"
                            >
                                {copied ? "Copied" : "Copy"}
                            </button>
                        </div>
                        <p className="text-xs text-text-subtle">
                            Scopes:{" "}
                            {created.scopes.includes("*")
                                ? "all"
                                : created.scopes.join(", ") || "none"}
                        </p>
                        <div className="flex justify-end">
                            <button
                                type="button"
                                onClick={closeAll}
                                className="rounded-md border border-border px-3 py-1.5 text-xs"
                            >
                                Done
                            </button>
                        </div>
                    </div>
                ) : (
                    <form onSubmit={onSubmit} className="space-y-3">
                        <label
                            className="block text-sm font-medium"
                            htmlFor="apikey-name"
                        >
                            Label (optional)
                            <input
                                id="apikey-name"
                                type="text"
                                className="mt-1 w-full rounded-md border border-border bg-bg-raised px-3 py-2 text-sm"
                                placeholder="e.g. Internal CRM sync job"
                                value={name}
                                onChange={(e) => setName(e.target.value)}
                            />
                        </label>

                        <fieldset>
                            <legend className="text-sm font-medium">
                                Scopes
                            </legend>
                            <p className="mt-1 mb-2 text-xs text-text-subtle">
                                Pick the operations this key can perform.
                                "Read-only" defaults are pre-checked. Use the
                                "All scopes" preset only for trusted internal
                                integrations.
                            </p>
                            {pickedScopes.has("*") ? (
                                <div className="rounded-md border border-accent-amber bg-accent-amber/10 px-3 py-2 text-xs">
                                    All-scopes preset selected. This key will
                                    have full tenant access.{" "}
                                    <button
                                        type="button"
                                        className="text-primary hover:underline"
                                        onClick={() =>
                                            setPickedScopes(
                                                new Set(defaultReadOnly),
                                            )
                                        }
                                    >
                                        Reset to read-only defaults
                                    </button>
                                </div>
                            ) : (
                                <div className="grid grid-cols-2 gap-1 max-h-60 overflow-y-auto rounded-md border border-border p-2">
                                    {allScopes
                                        .filter((s) => s !== "*")
                                        .map((s) => (
                                            <label
                                                key={s}
                                                className="flex items-center gap-2 text-xs"
                                            >
                                                <input
                                                    type="checkbox"
                                                    checked={pickedScopes.has(
                                                        s,
                                                    )}
                                                    onChange={() =>
                                                        togglePicked(s)
                                                    }
                                                />
                                                <span className="font-mono">
                                                    {s}
                                                </span>
                                            </label>
                                        ))}
                                </div>
                            )}
                            <div className="mt-2 flex gap-2 text-xs">
                                <button
                                    type="button"
                                    onClick={() =>
                                        setPickedScopes(new Set(defaultReadOnly))
                                    }
                                    className="text-primary hover:underline"
                                >
                                    Read-only defaults
                                </button>
                                <button
                                    type="button"
                                    onClick={onAllScopes}
                                    className="text-accent-amber hover:underline"
                                >
                                    All scopes
                                </button>
                            </div>
                        </fieldset>

                        {create.isError ? (
                            <p className="text-xs text-accent-rose">
                                {humanizeError(create.error)}
                            </p>
                        ) : null}
                        <div className="flex justify-end gap-2">
                            <button
                                type="button"
                                onClick={closeAll}
                                className="rounded-md border border-border px-3 py-1.5 text-xs"
                            >
                                Cancel
                            </button>
                            <button
                                type="submit"
                                disabled={create.isPending}
                                className="rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-white disabled:opacity-50"
                            >
                                {create.isPending ? "Creating…" : "Create"}
                            </button>
                        </div>
                    </form>
                )}
            </Modal>

            {/* All-scopes confirmation */}
            <Modal
                open={showAllConfirm}
                onClose={() => setShowAllConfirm(false)}
                title="Grant all scopes?"
            >
                <div className="space-y-3 text-sm">
                    <p>
                        The "All scopes" preset gives this key full tenant
                        access — every read and write surface, including user
                        management and GDPR delete. Use it only for trusted
                        internal integrations.
                    </p>
                    <div className="flex justify-end gap-2">
                        <button
                            type="button"
                            onClick={() => setShowAllConfirm(false)}
                            className="rounded-md border border-border px-3 py-1.5 text-xs"
                        >
                            Cancel
                        </button>
                        <button
                            type="button"
                            onClick={confirmAllScopes}
                            className="rounded-md bg-accent-amber px-3 py-1.5 text-xs font-medium text-white"
                        >
                            Yes, grant all scopes
                        </button>
                    </div>
                </div>
            </Modal>

            {/* Edit existing key's scopes */}
            <Modal
                open={editing !== null}
                onClose={() => setEditing(null)}
                title={`Edit key — ${editing?.name ?? editing?.id ?? ""}`}
            >
                <div className="space-y-3">
                    <p className="text-xs text-text-subtle">
                        Adjust scopes. The plaintext key never changes —
                        revoke + recreate to rotate the secret material.
                    </p>
                    {editScopes.has("*") ? (
                        <div className="rounded-md border border-accent-amber bg-accent-amber/10 px-3 py-2 text-xs">
                            All scopes selected.{" "}
                            <button
                                type="button"
                                className="text-primary hover:underline"
                                onClick={() =>
                                    setEditScopes(new Set(defaultReadOnly))
                                }
                            >
                                Reset to read-only defaults
                            </button>
                        </div>
                    ) : (
                        <div className="grid grid-cols-2 gap-1 max-h-60 overflow-y-auto rounded-md border border-border p-2">
                            {allScopes
                                .filter((s) => s !== "*")
                                .map((s) => (
                                    <label
                                        key={s}
                                        className="flex items-center gap-2 text-xs"
                                    >
                                        <input
                                            type="checkbox"
                                            checked={editScopes.has(s)}
                                            onChange={() =>
                                                setEditScopes((prev) => {
                                                    const next = new Set(prev);
                                                    if (next.has(s))
                                                        next.delete(s);
                                                    else next.add(s);
                                                    return next;
                                                })
                                            }
                                        />
                                        <span className="font-mono">{s}</span>
                                    </label>
                                ))}
                        </div>
                    )}
                    <div className="flex flex-wrap gap-2 text-xs">
                        <button
                            type="button"
                            onClick={() => setEditScopes(new Set(["*"]))}
                            className="text-accent-amber hover:underline"
                        >
                            All scopes
                        </button>
                        <button
                            type="button"
                            onClick={() =>
                                setEditScopes(new Set(defaultReadOnly))
                            }
                            className="text-primary hover:underline"
                        >
                            Read-only defaults
                        </button>
                    </div>
                    {update.isError ? (
                        <p className="text-xs text-accent-rose">
                            {humanizeError(update.error)}
                        </p>
                    ) : null}
                    <div className="flex justify-end gap-2">
                        <button
                            type="button"
                            onClick={() => setEditing(null)}
                            className="rounded-md border border-border px-3 py-1.5 text-xs"
                        >
                            Cancel
                        </button>
                        <button
                            type="button"
                            onClick={saveEdit}
                            disabled={update.isPending}
                            className="rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-white disabled:opacity-50"
                        >
                            {update.isPending ? "Saving…" : "Save"}
                        </button>
                    </div>
                </div>
            </Modal>
        </div>
    );
}
