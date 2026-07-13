"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";
import {
    useActivateCampaign,
    useApproveDrafts,
    useCampaignMembers,
    useGenerateDrafts,
    useOutreachCampaign,
    usePatchMember,
    usePauseCampaign,
    type OutreachConfigShape,
    type OutreachMemberOut,
} from "@/lib/outreach";
const PAGE_SIZE = 50;

const FONT_LABELS: Record<string, string> = {
    arial: "Arial",
    helvetica: "Helvetica",
    georgia: "Georgia",
    times: "Times New Roman",
    verdana: "Verdana",
    tahoma: "Tahoma",
    trebuchet: "Trebuchet MS",
    courier: "Courier New",
};

function fmtDateTime(value: string | null | undefined): string {
    if (!value) return "-";
    const d = new Date(value);
    if (Number.isNaN(d.getTime())) return "-";
    return d.toLocaleString();
}

function fmtDate(value: string | null | undefined): string {
    if (!value) return "-";
    const d = new Date(value);
    if (Number.isNaN(d.getTime())) return "-";
    return d.toLocaleDateString();
}

export default function OutreachCampaignDetailPage() {
    const params = useParams<{ id: string }>();
    const id = params?.id;
    const [page, setPage] = useState(0);

    const campaign = useOutreachCampaign(id);
    const members = useCampaignMembers(id, {
        limit: PAGE_SIZE,
        offset: page * PAGE_SIZE,
    });
    const generateDrafts = useGenerateDrafts(id);
    const approveDrafts = useApproveDrafts(id);
    const activate = useActivateCampaign(id);
    const pause = usePauseCampaign(id);

    const [actionErr, setActionErr] = useState<string | null>(null);

    if (!id) return null;

    if (campaign.isLoading) {
        return (
            <div className="space-y-4">
                <div className="h-6 w-1/3 animate-pulse rounded bg-bg-card-hover" />
                <div className="h-40 animate-pulse rounded-lg bg-bg-card" />
            </div>
        );
    }

    if (campaign.error || !campaign.data) {
        return (
            <div className="space-y-3">
                <Link
                    href="/outreach"
                    className="text-sm text-primary hover:underline"
                >
                    ← Back to outreach
                </Link>
                <p className="text-accent-rose">
                    Couldn&apos;t load this campaign.
                </p>
            </div>
        );
    }

    const c = campaign.data;
    const items = members.data?.items ?? [];
    const total = members.data?.total ?? 0;
    const hasNext = (page + 1) * PAGE_SIZE < total;
    const hasPrev = page > 0;

    async function runAction(fn: () => Promise<unknown>) {
        setActionErr(null);
        try {
            await fn();
        } catch (e) {
            setActionErr(e instanceof Error ? e.message : "Action failed.");
        }
    }

    return (
        <div className="space-y-6">
            <div>
                <Link
                    href="/outreach"
                    className="text-sm text-primary hover:underline"
                >
                    ← Back to outreach
                </Link>
            </div>

            <header className="rounded-lg border border-border bg-bg-card p-5">
                <div className="flex flex-wrap items-start justify-between gap-4">
                    <div>
                        <div className="flex items-center gap-3">
                            <h2 className="text-2xl font-bold">{c.name}</h2>
                            <StatusPill status={c.status} />
                        </div>
                        <p className="mt-1 text-sm text-text-muted">
                            Created {fmtDate(c.created_at)} · {c.sent_count}{" "}
                            sent
                        </p>
                        {c.quota ? (
                            <p className="mt-1 text-xs text-text-subtle">
                                Sent today: {c.quota.sent_today} /{" "}
                                {c.quota.daily_limit} (campaign) · tenant{" "}
                                {c.quota.tenant_sent_today} /{" "}
                                {c.quota.tenant_daily_cap}
                            </p>
                        ) : null}
                    </div>
                    <div className="flex flex-wrap gap-2">
                        {c.status === "draft" || c.status === "paused" ? (
                            <>
                                <button
                                    type="button"
                                    onClick={() =>
                                        runAction(() =>
                                            generateDrafts.mutateAsync(null),
                                        )
                                    }
                                    disabled={generateDrafts.isPending}
                                    className="rounded-md border border-border px-3 py-2 text-sm hover:bg-bg-card-hover disabled:opacity-50"
                                >
                                    {generateDrafts.isPending
                                        ? "Generating…"
                                        : "Generate drafts"}
                                </button>
                                <button
                                    type="button"
                                    onClick={() =>
                                        runAction(() =>
                                            approveDrafts.mutateAsync({
                                                all: true,
                                            }),
                                        )
                                    }
                                    disabled={approveDrafts.isPending}
                                    className="rounded-md border border-border px-3 py-2 text-sm hover:bg-bg-card-hover disabled:opacity-50"
                                >
                                    {approveDrafts.isPending
                                        ? "Approving…"
                                        : "Approve all drafts"}
                                </button>
                                <button
                                    type="button"
                                    onClick={() =>
                                        runAction(() => activate.mutateAsync())
                                    }
                                    disabled={activate.isPending}
                                    className="rounded-md bg-primary px-3 py-2 text-sm font-medium text-white hover:bg-primary-hover disabled:opacity-50"
                                >
                                    {activate.isPending
                                        ? "Activating…"
                                        : "Activate"}
                                </button>
                            </>
                        ) : c.status === "active" ? (
                            <button
                                type="button"
                                onClick={() =>
                                    runAction(() => pause.mutateAsync())
                                }
                                disabled={pause.isPending}
                                className="rounded-md border border-accent-amber/40 px-3 py-2 text-sm text-accent-amber hover:bg-accent-amber/10 disabled:opacity-50"
                            >
                                {pause.isPending ? "Pausing…" : "Pause"}
                            </button>
                        ) : null}
                    </div>
                </div>
                {actionErr ? (
                    <p className="mt-3 text-sm text-accent-rose">
                        {actionErr}
                    </p>
                ) : null}

                <div className="mt-4 flex flex-wrap gap-2">
                    {Object.entries(c.member_states).map(([state, count]) => (
                        <span
                            key={state}
                            className="rounded-full border border-border bg-bg-secondary px-3 py-1 text-xs capitalize text-text-muted"
                        >
                            {state.replace(/_/g, " ")}: {count}
                        </span>
                    ))}
                    {c.skipped.length > 0 ? (
                        <span className="rounded-full border border-accent-amber/40 bg-accent-amber/10 px-3 py-1 text-xs text-accent-amber">
                            {c.skipped.length} skipped on enrollment
                        </span>
                    ) : null}
                </div>
            </header>

            <ConfigSummaryCard config={c.config} />

            <section className="rounded-lg border border-border bg-bg-card overflow-hidden">
                <div className="border-b border-border px-5 py-3">
                    <h3 className="text-sm font-semibold">Members</h3>
                </div>
                {members.isLoading && !members.data ? (
                    <div className="p-5">
                        <div className="h-4 w-40 animate-pulse rounded bg-bg-secondary" />
                    </div>
                ) : items.length === 0 ? (
                    <p className="p-5 text-sm text-text-subtle">
                        No prospects enrolled yet.
                    </p>
                ) : (
                    <table className="w-full text-sm">
                        <thead className="bg-bg-secondary text-text-subtle text-xs uppercase tracking-wide">
                            <tr>
                                <th className="px-4 py-2 text-left">
                                    Prospect
                                </th>
                                <th className="px-4 py-2 text-left">Email</th>
                                <th className="px-4 py-2 text-left">State</th>
                                <th className="px-4 py-2 text-left">Draft</th>
                                <th className="px-4 py-2 text-left">
                                    Touches
                                </th>
                                <th className="px-4 py-2 text-left">
                                    Next send
                                </th>
                            </tr>
                        </thead>
                        <tbody>
                            {items.map((m) => (
                                <MemberRow
                                    key={m.id}
                                    member={m}
                                    campaignId={id}
                                />
                            ))}
                        </tbody>
                    </table>
                )}
            </section>

            {items.length > 0 ? (
                <div className="flex items-center justify-between text-xs text-text-subtle">
                    <span>
                        {page * PAGE_SIZE + 1}-
                        {Math.min((page + 1) * PAGE_SIZE, total)} of {total}
                    </span>
                    <div className="flex gap-2">
                        <button
                            type="button"
                            onClick={() => setPage((p) => Math.max(0, p - 1))}
                            disabled={!hasPrev}
                            className="rounded-md border border-border px-3 py-1 hover:bg-bg-secondary disabled:opacity-50"
                        >
                            Prev
                        </button>
                        <button
                            type="button"
                            onClick={() => setPage((p) => p + 1)}
                            disabled={!hasNext}
                            className="rounded-md border border-border px-3 py-1 hover:bg-bg-secondary disabled:opacity-50"
                        >
                            Next
                        </button>
                    </div>
                </div>
            ) : null}
        </div>
    );
}

function StatusPill({ status }: { status: string }) {
    const tone =
        status === "active"
            ? "text-accent-emerald"
            : status === "paused"
              ? "text-accent-amber"
              : "text-text-subtle";
    return (
        <span
            className={`rounded-full border border-border px-3 py-1 text-xs font-medium capitalize ${tone}`}
        >
            {status}
        </span>
    );
}

function ConfigSummaryCard({ config }: { config: OutreachConfigShape }) {
    const t = config?.template;
    if (!t) return null;
    return (
        <section className="rounded-lg border border-border bg-bg-card p-5">
            <h3 className="text-sm font-semibold">Template</h3>
            <dl className="mt-3 grid grid-cols-1 gap-3 text-sm sm:grid-cols-2">
                <div>
                    <dt className="text-xs uppercase tracking-wide text-text-subtle">
                        Subject
                    </dt>
                    <dd className="mt-1">{t.subject}</dd>
                </div>
                <div>
                    <dt className="text-xs uppercase tracking-wide text-text-subtle">
                        Font
                    </dt>
                    <dd className="mt-1">
                        {t.font_family
                            ? FONT_LABELS[t.font_family] ?? t.font_family
                            : "Default"}
                        {t.font_size_px ? ` · ${t.font_size_px}px` : ""}
                    </dd>
                </div>
                <div>
                    <dt className="text-xs uppercase tracking-wide text-text-subtle">
                        Logo
                    </dt>
                    <dd className="mt-1">
                        {t.include_logo ? "Included" : "Not included"}
                    </dd>
                </div>
                <div>
                    <dt className="text-xs uppercase tracking-wide text-text-subtle">
                        Attachments
                    </dt>
                    <dd className="mt-1">
                        {config.attachments.length > 0
                            ? config.attachments
                                  .map((a) => a.filename)
                                  .join(", ")
                            : "None"}
                    </dd>
                </div>
                <div>
                    <dt className="text-xs uppercase tracking-wide text-text-subtle">
                        Sequence
                    </dt>
                    <dd className="mt-1">
                        {config.steps.length} step
                        {config.steps.length === 1 ? "" : "s"} · max{" "}
                        {config.max_touches} touches · {config.mode}
                    </dd>
                </div>
                <div>
                    <dt className="text-xs uppercase tracking-wide text-text-subtle">
                        Send window
                    </dt>
                    <dd className="mt-1">
                        {config.send_window.start_hour}:00–
                        {config.send_window.end_hour}:00{" "}
                        {config.send_window.timezone}
                    </dd>
                </div>
            </dl>
        </section>
    );
}

function MemberRow({
    member,
    campaignId,
}: {
    member: OutreachMemberOut;
    campaignId: string;
}) {
    const [expanded, setExpanded] = useState(false);
    const [subject, setSubject] = useState(member.draft_subject ?? "");
    const [body, setBody] = useState(member.draft_body ?? "");
    const patchMember = usePatchMember(campaignId);
    const [err, setErr] = useState<string | null>(null);

    // draft_status is one of null (no draft), "ready" (awaiting review),
    // "approved" (queued to send) — see backend/app/services/outreach/
    // scheduler.py. Editing is only meaningful once a draft exists.
    const hasDraft = Boolean(member.draft_subject || member.draft_body);
    const needsReview = member.draft_status === "ready";

    function toggle() {
        if (!expanded) {
            setSubject(member.draft_subject ?? "");
            setBody(member.draft_body ?? "");
        }
        setExpanded((v) => !v);
    }

    async function handleSave(action?: "approve" | "reject") {
        setErr(null);
        try {
            await patchMember.mutateAsync({
                memberId: member.id,
                patch:
                    action === "reject"
                        ? { action: "reject" }
                        : {
                              draft_subject: subject,
                              draft_body: body,
                              ...(action === "approve"
                                  ? { action: "approve" as const }
                                  : {}),
                          },
            });
            if (action === "reject") setExpanded(false);
        } catch (e) {
            setErr(e instanceof Error ? e.message : "Couldn't save the draft.");
        }
    }

    return (
        <>
            <tr
                onClick={hasDraft ? toggle : undefined}
                className={`border-t border-border ${hasDraft ? "cursor-pointer hover:bg-bg-secondary" : ""}`}
            >
                <td className="px-4 py-3 font-medium">
                    {member.prospect_name ?? "-"}
                </td>
                <td className="px-4 py-3 text-text-muted">
                    {member.contact_email ?? "-"}
                </td>
                <td className="px-4 py-3">
                    <span className="text-xs capitalize text-text-muted">
                        {member.state.replace(/_/g, " ")}
                    </span>
                    {member.halt_reason ? (
                        <div className="mt-0.5 text-xs text-accent-rose">
                            {member.halt_reason.replace(/_/g, " ")}
                        </div>
                    ) : null}
                </td>
                <td className="px-4 py-3 text-xs capitalize text-text-muted">
                    {member.draft_status
                        ? member.draft_status.replace(/_/g, " ")
                        : "-"}
                </td>
                <td className="px-4 py-3 text-text-muted">
                    {member.touches_sent}
                </td>
                <td className="px-4 py-3 text-text-muted">
                    {fmtDateTime(member.next_send_at)}
                </td>
            </tr>
            {expanded ? (
                <tr className="border-t border-border bg-bg-secondary">
                    <td colSpan={6} className="px-4 py-3">
                        <div className="space-y-3">
                            <label className="block text-sm">
                                <span className="text-xs uppercase tracking-wide text-text-subtle">
                                    Subject
                                </span>
                                <input
                                    type="text"
                                    value={subject}
                                    onChange={(e) =>
                                        setSubject(e.target.value)
                                    }
                                    className="mt-1 w-full rounded-md border border-border bg-bg-card px-3 py-2 text-sm outline-none focus:border-primary"
                                />
                            </label>
                            <label className="block text-sm">
                                <span className="text-xs uppercase tracking-wide text-text-subtle">
                                    Body
                                </span>
                                <textarea
                                    value={body}
                                    onChange={(e) => setBody(e.target.value)}
                                    rows={8}
                                    className="mt-1 w-full whitespace-pre-wrap rounded-md border border-border bg-bg-card px-3 py-2 text-sm outline-none focus:border-primary"
                                />
                            </label>
                            <div className="flex flex-wrap items-center gap-2">
                                <button
                                    type="button"
                                    onClick={() => handleSave()}
                                    disabled={patchMember.isPending}
                                    className="rounded-md border border-border px-3 py-1.5 text-xs hover:bg-bg-card-hover disabled:opacity-50"
                                >
                                    Save edits
                                </button>
                                {needsReview ? (
                                    <>
                                        <button
                                            type="button"
                                            onClick={() =>
                                                handleSave("approve")
                                            }
                                            disabled={patchMember.isPending}
                                            className="rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-white hover:bg-primary-hover disabled:opacity-50"
                                        >
                                            Approve
                                        </button>
                                        <button
                                            type="button"
                                            onClick={() =>
                                                handleSave("reject")
                                            }
                                            disabled={patchMember.isPending}
                                            className="rounded-md border border-accent-rose/40 px-3 py-1.5 text-xs text-accent-rose hover:bg-accent-rose/10 disabled:opacity-50"
                                        >
                                            Reject
                                        </button>
                                    </>
                                ) : member.draft_status === "approved" ? (
                                    <span className="text-xs text-text-subtle">
                                        Approved — queued to send. Editing
                                        will send it back for re-approval.
                                    </span>
                                ) : null}
                            </div>
                            {err ? (
                                <p className="text-xs text-accent-rose">
                                    {err}
                                </p>
                            ) : null}
                        </div>
                    </td>
                </tr>
            ) : null}
        </>
    );
}
