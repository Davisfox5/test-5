"use client";

import { useState } from "react";
import {
    ApiKeyCreated,
    useApiKeys,
    useCreateApiKey,
    useRevokeApiKey,
} from "@/lib/api-keys";
import { Modal } from "@/components/admin/modal";
import { humanizeError } from "@/components/admin/section";

export function ApiKeysSection() {
    const { data: keys, isLoading, error } = useApiKeys();
    const create = useCreateApiKey();
    const revoke = useRevokeApiKey();

    const [showCreate, setShowCreate] = useState(false);
    const [name, setName] = useState("");
    const [created, setCreated] = useState<ApiKeyCreated | null>(null);
    const [copied, setCopied] = useState(false);

    const onSubmit = async (e: React.FormEvent) => {
        e.preventDefault();
        try {
            const out = await create.mutateAsync({ name: name || undefined });
            setCreated(out);
            setName("");
        } catch {
            // mutation surface its own error below
        }
    };

    const closeAll = () => {
        setShowCreate(false);
        setCreated(null);
        setCopied(false);
    };

    return (
        <div className="space-y-3">
            <div className="flex justify-end">
                <button
                    type="button"
                    onClick={() => setShowCreate(true)}
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
                                <td className="py-2 text-xs text-text-muted">
                                    {k.scopes.join(", ") || "—"}
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
                                <td className="py-2 text-right">
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
        </div>
    );
}
