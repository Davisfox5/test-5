"use client";

import { useAuth } from "@clerk/nextjs";
import { useEffect, useState } from "react";
import {
    useIngestRecording,
    useUploadInteraction,
    type UploadProgress,
} from "@/lib/interactions";

type Tab = "file" | "url";

export function UploadModal({
    open,
    onClose,
    onUploaded,
}: {
    open: boolean;
    onClose: () => void;
    onUploaded?: () => void;
}) {
    const [tab, setTab] = useState<Tab>("file");
    const [file, setFile] = useState<File | null>(null);
    const [title, setTitle] = useState("");
    const [audioUrl, setAudioUrl] = useState("");
    const [progress, setProgress] = useState<UploadProgress | null>(null);
    const [err, setErr] = useState<string | null>(null);
    const [toast, setToast] = useState<string | null>(null);

    const { getToken } = useAuth();
    const upload = useUploadInteraction();
    const ingest = useIngestRecording();

    useEffect(() => {
        if (!open) {
            setFile(null);
            setTitle("");
            setAudioUrl("");
            setProgress(null);
            setErr(null);
            setTab("file");
        }
    }, [open]);

    if (!open) return null;

    const submitting = upload.isPending || ingest.isPending;

    async function handleSubmit() {
        setErr(null);
        try {
            if (tab === "file") {
                if (!file) {
                    setErr("Pick an audio file first.");
                    return;
                }
                await upload.mutateAsync({
                    file,
                    title: title || undefined,
                    getToken,
                    onProgress: setProgress,
                });
            } else {
                if (!audioUrl) {
                    setErr("Paste a recording URL.");
                    return;
                }
                if (!/^https?:\/\//i.test(audioUrl)) {
                    setErr("URL must start with http:// or https://");
                    return;
                }
                await ingest.mutateAsync({
                    audio_url: audioUrl,
                    title: title || undefined,
                });
            }
            setToast("Upload received — Linda is processing it now.");
            onUploaded?.();
            // Brief pause so the toast registers before the modal closes.
            setTimeout(() => {
                setToast(null);
                onClose();
            }, 900);
        } catch (e) {
            setErr(e instanceof Error ? e.message : "Upload failed.");
            setProgress(null);
        }
    }

    return (
        <div
            className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 px-4"
            onClick={onClose}
        >
            <div
                className="w-full max-w-lg rounded-lg border border-border bg-bg-card shadow-lg"
                onClick={(e) => e.stopPropagation()}
                role="dialog"
                aria-modal="true"
            >
                <div className="flex items-center justify-between border-b border-border px-5 py-4">
                    <h3 className="text-lg font-semibold">Upload call</h3>
                    <button
                        type="button"
                        onClick={onClose}
                        className="text-text-subtle hover:text-text"
                        aria-label="Close"
                    >
                        ✕
                    </button>
                </div>

                <div className="px-5 pt-4">
                    <div
                        role="tablist"
                        className="inline-flex rounded-md border border-border bg-bg-secondary p-1 text-sm"
                    >
                        <TabButton
                            active={tab === "file"}
                            onClick={() => setTab("file")}
                        >
                            Upload audio file
                        </TabButton>
                        <TabButton
                            active={tab === "url"}
                            onClick={() => setTab("url")}
                        >
                            Paste recording URL
                        </TabButton>
                    </div>
                </div>

                <div className="space-y-4 px-5 py-4">
                    <Field label="Title (optional)">
                        <input
                            type="text"
                            value={title}
                            onChange={(e) => setTitle(e.target.value)}
                            disabled={submitting}
                            placeholder="e.g. Acme Corp discovery call"
                            className="w-full rounded-md border border-border bg-bg-secondary px-3 py-2 text-sm outline-none focus:border-primary"
                        />
                    </Field>

                    {tab === "file" ? (
                        <Field label="Audio file">
                            <input
                                type="file"
                                accept="audio/*,video/mp4,video/webm"
                                disabled={submitting}
                                onChange={(e) =>
                                    setFile(e.target.files?.[0] ?? null)
                                }
                                className="block w-full text-sm text-text-muted file:mr-3 file:rounded-md file:border-0 file:bg-primary file:px-3 file:py-2 file:text-sm file:font-medium file:text-white hover:file:bg-primary-hover"
                            />
                            <p className="mt-1 text-xs text-text-subtle">
                                mp3, m4a, wav, webm, or mp4 video (max 500MB).
                            </p>
                            {file ? (
                                <p className="mt-2 text-xs text-text-muted">
                                    {file.name} ·{" "}
                                    {(file.size / (1024 * 1024)).toFixed(1)}MB
                                </p>
                            ) : null}
                            {progress ? (
                                <div className="mt-3">
                                    <div className="h-2 w-full overflow-hidden rounded-full bg-bg-secondary">
                                        <div
                                            className="h-full bg-primary transition-[width]"
                                            style={{
                                                width: `${progress.percent}%`,
                                            }}
                                        />
                                    </div>
                                    <p className="mt-1 text-xs text-text-subtle">
                                        Uploading… {progress.percent}%
                                    </p>
                                </div>
                            ) : null}
                        </Field>
                    ) : (
                        <Field label="Recording URL">
                            <input
                                type="url"
                                value={audioUrl}
                                onChange={(e) => setAudioUrl(e.target.value)}
                                disabled={submitting}
                                placeholder="https://example.com/recordings/abc.mp3"
                                className="w-full rounded-md border border-border bg-bg-secondary px-3 py-2 text-sm outline-none focus:border-primary"
                            />
                            <p className="mt-1 text-xs text-text-subtle">
                                Public or pre-signed mp3 / m4a / wav / webm
                                URL. Linda fetches it and runs the analysis.
                            </p>
                        </Field>
                    )}

                    {err ? (
                        <div className="rounded-md border border-accent-rose/40 bg-accent-rose/10 px-3 py-2 text-sm text-accent-rose">
                            {err}
                        </div>
                    ) : null}
                    {toast ? (
                        <div className="rounded-md border border-accent-emerald/40 bg-accent-emerald/10 px-3 py-2 text-sm text-accent-emerald">
                            {toast}
                        </div>
                    ) : null}
                </div>

                <div className="flex items-center justify-end gap-2 border-t border-border px-5 py-3">
                    <button
                        type="button"
                        className="rounded-md border border-border px-3 py-2 text-sm text-text-muted hover:bg-bg-card-hover"
                        onClick={onClose}
                        disabled={submitting}
                    >
                        Cancel
                    </button>
                    <button
                        type="button"
                        className="rounded-md bg-primary px-3 py-2 text-sm font-medium text-white hover:bg-primary-hover disabled:cursor-not-allowed disabled:opacity-60"
                        onClick={handleSubmit}
                        disabled={submitting}
                    >
                        {submitting
                            ? "Uploading…"
                            : tab === "file"
                              ? "Upload"
                              : "Ingest"}
                    </button>
                </div>
            </div>
        </div>
    );
}

function TabButton({
    active,
    onClick,
    children,
}: {
    active: boolean;
    onClick: () => void;
    children: React.ReactNode;
}) {
    return (
        <button
            type="button"
            role="tab"
            aria-selected={active}
            onClick={onClick}
            className={`rounded px-3 py-1.5 text-sm transition-colors ${
                active
                    ? "bg-bg-card text-text shadow-sm"
                    : "text-text-muted hover:text-text"
            }`}
        >
            {children}
        </button>
    );
}

function Field({
    label,
    children,
}: {
    label: string;
    children: React.ReactNode;
}) {
    return (
        <label className="block">
            <span className="mb-1 block text-sm font-medium text-text-muted">
                {label}
            </span>
            {children}
        </label>
    );
}
