"use client";

import { useState } from "react";
import {
    Webhook,
    WebhookCreated,
    useCreateWebhook,
    useDeleteWebhook,
    usePatchWebhook,
    useTestWebhook,
    useWebhookEvents,
    useWebhooks,
} from "@/lib/webhooks";
import { Modal } from "@/components/admin/modal";
import { humanizeError } from "@/components/admin/section";

export function WebhooksSection() {
    const { data: webhooks, isLoading, error } = useWebhooks();
    const { data: eventsResp } = useWebhookEvents();
    const create = useCreateWebhook();
    const patch = usePatchWebhook();
    const remove = useDeleteWebhook();
    const test = useTestWebhook();

    const [showCreate, setShowCreate] = useState(false);
    const [editing, setEditing] = useState<Webhook | null>(null);
    const [created, setCreated] = useState<WebhookCreated | null>(null);
    const [testResult, setTestResult] = useState<
        Record<string, string | null>
    >({});

    const allEvents = eventsResp?.events ?? [];

    const onTest = async (id: string) => {
        try {
            const r = await test.mutateAsync(id);
            const msg =
                r.status === "delivered"
                    ? `Delivered (${r.status_code ?? ""})`
                    : `Failed: ${r.error ?? "unknown"}`;
            setTestResult((prev) => ({ ...prev, [id]: msg }));
        } catch (err) {
            setTestResult((prev) => ({
                ...prev,
                [id]: humanizeError(err),
            }));
        }
    };

    return (
        <div className="space-y-3">
            <div className="flex justify-end">
                <button
                    type="button"
                    onClick={() => setShowCreate(true)}
                    className="rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-white"
                >
                    New webhook
                </button>
            </div>

            {isLoading ? (
                <p className="text-sm text-text-muted">Loading webhooks…</p>
            ) : error ? (
                <p className="text-sm text-accent-rose">
                    {humanizeError(error)}
                </p>
            ) : !webhooks?.length ? (
                <p className="text-sm text-text-muted">
                    No webhooks configured. Add one above to receive event
                    callbacks.
                </p>
            ) : (
                <ul className="space-y-2">
                    {webhooks.map((wh) => (
                        <li
                            key={wh.id}
                            className="rounded-md border border-border bg-bg-raised p-3"
                        >
                            <div className="flex flex-wrap items-start justify-between gap-2">
                                <div className="min-w-0 flex-1">
                                    <div className="truncate font-mono text-sm">
                                        {wh.url}
                                    </div>
                                    <div className="mt-1 text-xs text-text-subtle">
                                        Events:{" "}
                                        {wh.events.length
                                            ? wh.events.join(", ")
                                            : "*"}{" "}
                                        · {wh.active ? "Active" : "Paused"}
                                    </div>
                                    {testResult[wh.id] ? (
                                        <div className="mt-1 text-xs text-text-muted">
                                            Test: {testResult[wh.id]}
                                        </div>
                                    ) : null}
                                </div>
                                <div className="flex gap-2 text-xs">
                                    <button
                                        type="button"
                                        onClick={() => onTest(wh.id)}
                                        disabled={test.isPending}
                                        className="rounded-md border border-border bg-bg-card px-2 py-1 disabled:opacity-50"
                                    >
                                        {test.isPending &&
                                        test.variables === wh.id
                                            ? "Testing…"
                                            : "Send test"}
                                    </button>
                                    <button
                                        type="button"
                                        onClick={() => setEditing(wh)}
                                        className="rounded-md border border-border bg-bg-card px-2 py-1"
                                    >
                                        Edit
                                    </button>
                                    <button
                                        type="button"
                                        onClick={() =>
                                            patch.mutate({
                                                id: wh.id,
                                                patch: { active: !wh.active },
                                            })
                                        }
                                        className="rounded-md border border-border bg-bg-card px-2 py-1"
                                    >
                                        {wh.active ? "Pause" : "Resume"}
                                    </button>
                                    <button
                                        type="button"
                                        onClick={() => {
                                            if (
                                                confirm(
                                                    `Delete webhook ${wh.url}?`,
                                                )
                                            )
                                                remove.mutate(wh.id);
                                        }}
                                        className="rounded-md border border-border bg-bg-card px-2 py-1 text-accent-rose"
                                    >
                                        Delete
                                    </button>
                                </div>
                            </div>
                        </li>
                    ))}
                </ul>
            )}

            <Modal
                open={showCreate}
                onClose={() => {
                    setShowCreate(false);
                    setCreated(null);
                }}
                title={
                    created
                        ? "Save your webhook secret"
                        : "Create a webhook"
                }
            >
                {created ? (
                    <CreatedSecret
                        secret={created.secret}
                        onClose={() => {
                            setShowCreate(false);
                            setCreated(null);
                        }}
                    />
                ) : (
                    <WebhookForm
                        events={allEvents}
                        submitting={create.isPending}
                        error={
                            create.isError
                                ? humanizeError(create.error)
                                : null
                        }
                        onSubmit={async (payload) => {
                            const out = await create.mutateAsync(payload);
                            setCreated(out);
                        }}
                        onCancel={() => setShowCreate(false)}
                    />
                )}
            </Modal>

            <Modal
                open={!!editing}
                onClose={() => setEditing(null)}
                title="Edit webhook"
            >
                {editing ? (
                    <WebhookForm
                        events={allEvents}
                        initial={{
                            url: editing.url,
                            events: editing.events,
                            active: editing.active,
                        }}
                        submitting={patch.isPending}
                        error={
                            patch.isError ? humanizeError(patch.error) : null
                        }
                        onSubmit={async (payload) => {
                            await patch.mutateAsync({
                                id: editing.id,
                                patch: payload,
                            });
                            setEditing(null);
                        }}
                        onCancel={() => setEditing(null)}
                    />
                ) : null}
            </Modal>
        </div>
    );
}

function WebhookForm({
    events,
    initial,
    submitting,
    error,
    onSubmit,
    onCancel,
}: {
    events: Array<{ name: string; description: string }>;
    initial?: { url: string; events: string[]; active: boolean };
    submitting: boolean;
    error: string | null;
    onSubmit: (payload: {
        url: string;
        events: string[];
        active: boolean;
    }) => Promise<void> | void;
    onCancel: () => void;
}) {
    const [url, setUrl] = useState(initial?.url ?? "");
    const [selected, setSelected] = useState<string[]>(
        initial?.events ?? ["*"],
    );
    const [active, setActive] = useState(initial?.active ?? true);

    const toggleEvent = (name: string) => {
        setSelected((prev) =>
            prev.includes(name)
                ? prev.filter((n) => n !== name)
                : [...prev.filter((n) => n !== "*"), name],
        );
    };

    return (
        <form
            onSubmit={(e) => {
                e.preventDefault();
                onSubmit({
                    url: url.trim(),
                    events: selected.length ? selected : ["*"],
                    active,
                });
            }}
            className="space-y-3"
        >
            <label className="block text-sm font-medium">
                Receiver URL
                <input
                    type="url"
                    required
                    value={url}
                    onChange={(e) => setUrl(e.target.value)}
                    placeholder="https://example.com/webhooks/linda"
                    className="mt-1 w-full rounded-md border border-border bg-bg-raised px-3 py-2 text-sm"
                />
            </label>
            <fieldset>
                <legend className="text-sm font-medium">Events</legend>
                <label className="mt-2 flex items-center gap-2 text-sm">
                    <input
                        type="checkbox"
                        checked={selected.includes("*")}
                        onChange={() =>
                            setSelected(
                                selected.includes("*") ? [] : ["*"],
                            )
                        }
                    />
                    All events (
                    <code>*</code>)
                </label>
                <div className="mt-2 max-h-40 space-y-1 overflow-y-auto pr-2">
                    {events.map((ev) => (
                        <label
                            key={ev.name}
                            className="flex items-start gap-2 text-xs"
                        >
                            <input
                                type="checkbox"
                                disabled={selected.includes("*")}
                                checked={selected.includes(ev.name)}
                                onChange={() => toggleEvent(ev.name)}
                            />
                            <span>
                                <span className="font-mono">{ev.name}</span>
                                <span className="ml-1 text-text-subtle">
                                    {ev.description}
                                </span>
                            </span>
                        </label>
                    ))}
                </div>
            </fieldset>
            <label className="flex items-center gap-2 text-sm">
                <input
                    type="checkbox"
                    checked={active}
                    onChange={(e) => setActive(e.target.checked)}
                />
                Active
            </label>
            {error ? (
                <p className="text-xs text-accent-rose">{error}</p>
            ) : null}
            <div className="flex justify-end gap-2">
                <button
                    type="button"
                    onClick={onCancel}
                    className="rounded-md border border-border px-3 py-1.5 text-xs"
                >
                    Cancel
                </button>
                <button
                    type="submit"
                    disabled={submitting}
                    className="rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-white disabled:opacity-50"
                >
                    {submitting ? "Saving…" : "Save"}
                </button>
            </div>
        </form>
    );
}

function CreatedSecret({
    secret,
    onClose,
}: {
    secret: string;
    onClose: () => void;
}) {
    const [copied, setCopied] = useState(false);
    return (
        <div className="space-y-3">
            <p className="text-sm text-text-muted">
                Save this HMAC secret now — it will not be shown again.
            </p>
            <div className="flex items-center gap-2">
                <code className="flex-1 truncate rounded-md border border-border bg-bg-raised px-3 py-2 font-mono text-xs">
                    {secret}
                </code>
                <button
                    type="button"
                    onClick={() => {
                        navigator.clipboard.writeText(secret);
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
                    onClick={onClose}
                    className="rounded-md border border-border px-3 py-1.5 text-xs"
                >
                    Done
                </button>
            </div>
        </div>
    );
}
