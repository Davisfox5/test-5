"use client";

import Link from "next/link";
import { useRef, useState } from "react";
import {
    useDeleteEmailLogo,
    useEmailLogo,
    useOutreachCampaigns,
    useUploadEmailLogo,
    type OutreachCampaignOut,
} from "@/lib/outreach";

function fmtDate(value: string | null | undefined): string {
    if (!value) return "-";
    const d = new Date(value);
    if (Number.isNaN(d.getTime())) return "-";
    return d.toLocaleDateString();
}

function summarizeMemberStates(states: Record<string, number>): string {
    // Mirrors the state machine in services/outreach/scheduler.py +
    // replies.py: draft_pending -> needs_approval -> queued ->
    // in_sequence -> completed, with replied / opted_out / bounced /
    // halted / failed able to interrupt at any point.
    const inSequence = (states.in_sequence ?? 0) + (states.queued ?? 0);
    const parts: string[] = [];
    if (inSequence > 0) parts.push(`${inSequence} in sequence`);
    if ((states.needs_approval ?? 0) > 0)
        parts.push(`${states.needs_approval} awaiting review`);
    if ((states.draft_pending ?? 0) > 0)
        parts.push(`${states.draft_pending} pending drafts`);
    if ((states.replied ?? 0) > 0) parts.push(`${states.replied} replied`);
    if ((states.completed ?? 0) > 0) parts.push(`${states.completed} completed`);
    if ((states.opted_out ?? 0) > 0)
        parts.push(`${states.opted_out} opted out`);
    if ((states.bounced ?? 0) > 0) parts.push(`${states.bounced} bounced`);
    if ((states.halted ?? 0) > 0) parts.push(`${states.halted} halted`);
    if ((states.failed ?? 0) > 0) parts.push(`${states.failed} failed`);
    return parts.length > 0 ? parts.join(" · ") : "No members enrolled";
}

export default function OutreachListPage() {
    const { data, isLoading, error } = useOutreachCampaigns();
    const campaigns = data ?? [];

    return (
        <div className="space-y-6">
            <header className="flex flex-wrap items-center justify-between gap-3">
                <div>
                    <h2 className="text-2xl font-bold">Outreach</h2>
                    <p className="text-text-muted mt-1">
                        Cold-outreach campaigns Linda drafts and sends on your
                        behalf.
                    </p>
                </div>
                <Link
                    href="/outreach/new"
                    className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-white hover:bg-primary-hover"
                >
                    New campaign
                </Link>
            </header>

            {error ? (
                <div className="rounded-lg border border-accent-rose bg-bg-card p-4 text-sm text-accent-rose">
                    Couldn&apos;t load campaigns:{" "}
                    {(error as Error).message}
                </div>
            ) : isLoading && !data ? (
                <Skeleton />
            ) : campaigns.length === 0 ? (
                <EmptyState />
            ) : (
                <section className="rounded-lg border border-border bg-bg-card overflow-hidden">
                    <table className="w-full text-sm">
                        <thead className="bg-bg-secondary text-text-subtle text-xs uppercase tracking-wide">
                            <tr>
                                <th className="px-4 py-2 text-left">Name</th>
                                <th className="px-4 py-2 text-left">Status</th>
                                <th className="px-4 py-2 text-left">Sent</th>
                                <th className="px-4 py-2 text-left">Members</th>
                                <th className="px-4 py-2 text-left">Created</th>
                            </tr>
                        </thead>
                        <tbody>
                            {campaigns.map((c) => (
                                <Row key={c.id} campaign={c} />
                            ))}
                        </tbody>
                    </table>
                </section>
            )}

            <EmailBrandingSection />
        </div>
    );
}

function Row({ campaign }: { campaign: OutreachCampaignOut }) {
    return (
        <tr className="border-t border-border hover:bg-bg-secondary">
            <td className="px-4 py-3">
                <Link
                    href={`/outreach/${campaign.id}`}
                    className="font-medium text-primary hover:underline"
                >
                    {campaign.name}
                </Link>
            </td>
            <td className="px-4 py-3">
                <StatusPill status={campaign.status} />
            </td>
            <td className="px-4 py-3 text-text-muted">
                {campaign.sent_count}
            </td>
            <td className="px-4 py-3 text-text-muted">
                {summarizeMemberStates(campaign.member_states)}
            </td>
            <td className="px-4 py-3 text-text-muted">
                {fmtDate(campaign.created_at)}
            </td>
        </tr>
    );
}

function StatusPill({ status }: { status: string }) {
    const tone =
        status === "active"
            ? "text-accent-emerald"
            : status === "paused"
              ? "text-accent-amber"
              : status === "draft" || status === "completed"
                ? "text-text-subtle"
                : "text-text-subtle";
    return (
        <span className={`text-xs font-medium capitalize ${tone}`}>
            {status}
        </span>
    );
}

function Skeleton() {
    return (
        <div className="rounded-lg border border-border bg-bg-card p-6 animate-pulse">
            <div className="h-4 w-40 bg-bg-secondary rounded mb-3" />
            <div className="h-3 w-2/3 bg-bg-secondary rounded mb-2" />
            <div className="h-3 w-1/2 bg-bg-secondary rounded" />
        </div>
    );
}

function EmptyState() {
    return (
        <div className="rounded-lg border border-border border-dashed bg-bg-card p-10 text-center space-y-2">
            <p className="text-text-muted">
                No campaigns yet — create your first outreach campaign.
            </p>
            <p className="text-text-subtle text-sm">
                <Link
                    href="/outreach/new"
                    className="text-primary hover:underline"
                >
                    Get started
                </Link>{" "}
                by writing an email template and picking prospects to enroll.
            </p>
        </div>
    );
}

function EmailBrandingSection() {
    const logo = useEmailLogo();
    const upload = useUploadEmailLogo();
    const del = useDeleteEmailLogo();
    const [err, setErr] = useState<string | null>(null);
    const fileInputRef = useRef<HTMLInputElement | null>(null);

    async function handleFile(file: File | null) {
        if (!file) return;
        setErr(null);
        try {
            await upload.mutateAsync(file);
        } catch (e) {
            setErr(e instanceof Error ? e.message : "Upload failed.");
        }
    }

    async function handleRemove() {
        setErr(null);
        try {
            await del.mutateAsync();
        } catch (e) {
            setErr(e instanceof Error ? e.message : "Couldn't remove logo.");
        }
    }

    return (
        <section className="rounded-lg border border-border bg-bg-card p-5">
            <h3 className="text-sm font-semibold">Email branding</h3>
            <p className="mt-1 text-xs text-text-muted">
                This logo is embedded at the bottom of every outreach email
                (when a campaign has &quot;Add logo&quot; enabled).
            </p>

            <div className="mt-4">
                {logo.isLoading ? (
                    <div className="h-16 w-32 animate-pulse rounded bg-bg-secondary" />
                ) : logo.data ? (
                    <div className="flex flex-wrap items-center gap-4">
                        {/* eslint-disable-next-line @next/next/no-img-element */}
                        <img
                            src={logo.data.url ?? undefined}
                            alt="Email logo"
                            className="max-h-16 rounded border border-border bg-white p-1"
                        />
                        <div className="text-xs text-text-muted">
                            {logo.data.filename}
                        </div>
                        <div className="flex gap-2">
                            <button
                                type="button"
                                onClick={() => fileInputRef.current?.click()}
                                disabled={upload.isPending}
                                className="rounded-md border border-border px-3 py-1.5 text-xs hover:bg-bg-card-hover disabled:opacity-50"
                            >
                                {upload.isPending ? "Uploading…" : "Replace"}
                            </button>
                            <button
                                type="button"
                                onClick={handleRemove}
                                disabled={del.isPending}
                                className="rounded-md border border-accent-rose/40 px-3 py-1.5 text-xs text-accent-rose hover:bg-accent-rose/10 disabled:opacity-50"
                            >
                                {del.isPending ? "Removing…" : "Remove"}
                            </button>
                        </div>
                    </div>
                ) : (
                    <div>
                        <input
                            type="file"
                            accept="image/png,image/jpeg,image/gif"
                            disabled={upload.isPending}
                            onChange={(e) =>
                                handleFile(e.target.files?.[0] ?? null)
                            }
                            className="block text-sm text-text-muted file:mr-3 file:rounded-md file:border-0 file:bg-primary file:px-3 file:py-2 file:text-sm file:font-medium file:text-white hover:file:bg-primary-hover"
                        />
                        <p className="mt-1 text-xs text-text-subtle">
                            PNG, JPEG, or GIF, up to 1 MB.
                        </p>
                    </div>
                )}
                <input
                    ref={fileInputRef}
                    type="file"
                    accept="image/png,image/jpeg,image/gif"
                    className="hidden"
                    onChange={(e) => handleFile(e.target.files?.[0] ?? null)}
                />
            </div>

            {err ? (
                <p className="mt-2 text-xs text-accent-rose">{err}</p>
            ) : null}
        </section>
    );
}
