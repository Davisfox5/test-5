"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import {
    KBDoc,
    useCreateKBDoc,
    useDeleteKBDoc,
    useUpdateKBDoc,
    useUploadKBFile,
} from "@/lib/knowledge-base";

export interface KBEditorProps {
    initial?: KBDoc;
    mode: "create" | "edit";
}

interface EditorState {
    title: string;
    body: string;
    tags: string[];
}

function fromDoc(doc: KBDoc | undefined): EditorState {
    return {
        title: doc?.title ?? "",
        body: doc?.content ?? "",
        tags: doc?.tags ?? [],
    };
}

export function KnowledgeBaseEditor({ initial, mode }: KBEditorProps) {
    const router = useRouter();
    const fileInput = useRef<HTMLInputElement | null>(null);

    const [state, setState] = useState<EditorState>(() => fromDoc(initial));
    const [tagDraft, setTagDraft] = useState("");
    const [confirmDelete, setConfirmDelete] = useState(false);
    const [uploadError, setUploadError] = useState<string | null>(null);

    const create = useCreateKBDoc();
    const update = useUpdateKBDoc();
    const del = useDeleteKBDoc();
    const upload = useUploadKBFile();

    useEffect(() => {
        if (initial) setState(fromDoc(initial));
    }, [initial]);

    const titleOk = state.title.trim().length > 0;
    const bodyOk = state.body.trim().length > 0;
    const valid = titleOk && bodyOk;

    function addTag() {
        const t = tagDraft.trim();
        if (!t) return;
        setState((s) =>
            s.tags.includes(t) ? s : { ...s, tags: [...s.tags, t] },
        );
        setTagDraft("");
    }

    function removeTag(t: string) {
        setState((s) => ({ ...s, tags: s.tags.filter((x) => x !== t) }));
    }

    async function handleUpload(file: File) {
        setUploadError(null);
        try {
            const doc = await upload.mutateAsync(file);
            // The /kb/upload endpoint creates a fresh doc — pull its
            // extracted content into the editor so the user can rename
            // the title/tags before re-saving (POST /kb/docs) if they
            // want a clean editor doc, or just navigate to the new
            // upload doc which already exists.
            setState({
                title: state.title || doc.title || file.name,
                body: doc.content ?? "",
                tags: state.tags.length ? state.tags : (doc.tags ?? []),
            });
        } catch (err) {
            setUploadError(
                err instanceof Error
                    ? err.message
                    : "Upload failed — try a smaller file.",
            );
        }
    }

    async function handleSave() {
        if (!valid) return;
        const payload = {
            title: state.title.trim(),
            content: state.body,
            tags: state.tags,
        };
        if (mode === "create") {
            const created = await create.mutateAsync({
                ...payload,
                source_type: "editor",
            });
            router.push(`/knowledge-base/${created.id}`);
        } else if (initial) {
            await update.mutateAsync({ id: initial.id, patch: payload });
        }
    }

    async function handleDelete() {
        if (!initial) return;
        await del.mutateAsync(initial.id);
        router.push("/knowledge-base");
    }

    const saving = create.isPending || update.isPending;
    const saveError = create.error ?? update.error;

    return (
        <div className="space-y-6">
            <header className="flex items-start justify-between gap-4 flex-wrap">
                <div className="flex-1 min-w-0 space-y-1">
                    <Link
                        href="/knowledge-base"
                        className="text-sm text-text-subtle hover:text-text-muted"
                    >
                        ← Back to knowledge base
                    </Link>
                    <label htmlFor="kb-title" className="sr-only">
                        Document title
                    </label>
                    <input
                        id="kb-title"
                        type="text"
                        value={state.title}
                        onChange={(e) =>
                            setState((s) => ({ ...s, title: e.target.value }))
                        }
                        placeholder="Document title"
                        aria-label="Document title"
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
                        disabled={!valid || saving}
                        className="rounded-md bg-primary text-white px-3 py-1.5 text-sm font-medium hover:bg-primary/90 disabled:opacity-50"
                    >
                        {saving ? "Saving…" : "Save"}
                    </button>
                </div>
            </header>

            <section className="rounded-lg border border-border bg-bg-card p-6 space-y-3">
                <h3 className="text-lg font-semibold">Tags</h3>
                <div className="flex flex-wrap gap-2">
                    {state.tags.map((t) => (
                        <span
                            key={t}
                            className="inline-flex items-center gap-1 rounded-md border border-border bg-bg-raised px-2 py-1 text-xs"
                        >
                            {t}
                            <button
                                type="button"
                                onClick={() => removeTag(t)}
                                aria-label={`Remove tag ${t}`}
                                className="text-text-subtle hover:text-accent-rose"
                            >
                                ×
                            </button>
                        </span>
                    ))}
                    {!state.tags.length ? (
                        <span className="text-xs text-text-subtle">
                            No tags yet.
                        </span>
                    ) : null}
                </div>
                <div className="flex gap-2">
                    <input
                        type="text"
                        value={tagDraft}
                        onChange={(e) => setTagDraft(e.target.value)}
                        onKeyDown={(e) => {
                            if (e.key === "Enter") {
                                e.preventDefault();
                                addTag();
                            }
                        }}
                        placeholder="Add tag (press enter)"
                        className="flex-1 rounded-md border border-border bg-bg-raised px-3 py-1.5 text-sm"
                    />
                    <button
                        type="button"
                        onClick={addTag}
                        className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-bg-secondary"
                    >
                        Add
                    </button>
                </div>
            </section>

            <section className="rounded-lg border border-border bg-bg-card p-6 space-y-3">
                <div className="flex items-start justify-between gap-3 flex-wrap">
                    <div>
                        <h3 className="text-lg font-semibold">Content</h3>
                        <p className="text-sm text-text-muted mt-1">
                            Plain text or markdown — Linda chunks and embeds
                            this on save.
                        </p>
                    </div>
                    <div className="flex flex-col items-end gap-1">
                        <button
                            type="button"
                            onClick={() => fileInput.current?.click()}
                            disabled={upload.isPending}
                            className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-bg-secondary disabled:opacity-50"
                        >
                            {upload.isPending
                                ? "Extracting…"
                                : "Upload file (txt / pdf / docx)"}
                        </button>
                        <input
                            ref={fileInput}
                            type="file"
                            accept=".txt,.pdf,.docx,text/plain,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                            className="hidden"
                            onChange={(e) => {
                                const f = e.target.files?.[0];
                                if (f) handleUpload(f);
                                if (fileInput.current) {
                                    fileInput.current.value = "";
                                }
                            }}
                        />
                        {uploadError ? (
                            <p className="text-xs text-accent-rose max-w-xs text-right">
                                {uploadError}
                            </p>
                        ) : null}
                    </div>
                </div>
                <textarea
                    value={state.body}
                    onChange={(e) =>
                        setState((s) => ({ ...s, body: e.target.value }))
                    }
                    placeholder="Paste or type the document body here. Upload a file above to extract its text."
                    rows={20}
                    aria-label="Document content"
                    className="w-full rounded-md border border-border bg-bg-elevated px-3 py-2 text-sm font-mono"
                />
            </section>

            {!valid ? (
                <div className="rounded-lg border border-accent-amber bg-bg-card p-3 text-sm text-accent-amber space-y-1">
                    {!titleOk ? <div>Title is required.</div> : null}
                    {!bodyOk ? (
                        <div>Body cannot be empty — paste text or upload a file.</div>
                    ) : null}
                </div>
            ) : null}

            {saveError ? (
                <div className="rounded-lg border border-accent-rose bg-bg-card p-3 text-sm text-accent-rose">
                    Save failed: {(saveError as Error).message}
                </div>
            ) : null}

            {confirmDelete && initial ? (
                <ConfirmDialog
                    title={`Delete "${initial.title ?? "this document"}"?`}
                    body="This is permanent. The document and its embeddings will be removed and Linda will rebuild the tenant context."
                    confirmLabel={del.isPending ? "Deleting…" : "Delete"}
                    onCancel={() => setConfirmDelete(false)}
                    onConfirm={handleDelete}
                />
            ) : null}
        </div>
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
