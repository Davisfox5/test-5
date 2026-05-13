"use client";

/**
 * Shared building blocks for the four customer-detail layouts.
 *
 * Layouts 1 / 2 / 3 / 4 differ in *framing* (how the same data is
 * arranged on screen), not in *what* data they show. Per the plan,
 * the user picks the winner after running them in parallel — keeping
 * the components honest means each layout composes from the same
 * building blocks here, not from layout-specific renderings.
 */

import Link from "next/link";
import { useState } from "react";
import {
    contactRoleLabel,
    faviconFor,
    useDismissWarning,
    useUpdateCommitment,
    warningKindLabel,
    type CommitmentOut,
    type CustomerActionItemSummary,
    type CustomerContactOut,
    type CustomerDetail,
    type CustomerInteractionSummary,
    type CustomerWarningOut,
} from "@/lib/customers";
import { formatRelative, sentimentLabel } from "@/lib/interactions";

export function CustomerLogo({
    domain,
    name,
    size = 36,
}: {
    domain: string | null;
    name: string;
    size?: number;
}) {
    const fav = faviconFor(domain);
    const initials =
        name
            .split(/\s+/)
            .filter(Boolean)
            .slice(0, 2)
            .map((t) => t[0]?.toUpperCase() ?? "")
            .join("") || "·";
    if (fav) {
        return (
            <div
                className="overflow-hidden rounded-md bg-bg-secondary"
                style={{ width: size, height: size }}
            >
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img
                    src={fav}
                    alt={`${domain ?? name} logo`}
                    style={{ width: size, height: size }}
                    className="object-cover"
                    loading="lazy"
                />
            </div>
        );
    }
    return (
        <div
            className="flex items-center justify-center rounded-md bg-primary-soft text-sm font-semibold text-primary"
            style={{ width: size, height: size }}
        >
            {initials}
        </div>
    );
}

export function ScoreCard({
    label,
    value,
    accentText,
    tone,
}: {
    label: string;
    value: string;
    accentText?: string;
    tone: "emerald" | "amber" | "rose" | "subtle";
}) {
    const toneCls =
        tone === "emerald"
            ? "text-accent-emerald"
            : tone === "amber"
              ? "text-accent-amber"
              : tone === "rose"
                ? "text-accent-rose"
                : "text-text-subtle";
    return (
        <div className="rounded-md border border-border bg-bg-secondary px-3 py-2 text-right">
            <div className="text-[10px] uppercase tracking-wide text-text-subtle">
                {label}
            </div>
            <div className="text-lg font-semibold">{value}</div>
            {accentText ? (
                <div className={`text-[11px] ${toneCls}`}>{accentText}</div>
            ) : null}
        </div>
    );
}

export function deriveScoreVisuals(c: CustomerDetail) {
    const sent = sentimentLabel(c.sentiment_score);
    const churnPct =
        c.churn_risk != null
            ? `${Math.round(c.churn_risk * 100)}%`
            : "—";
    const churnTone: "emerald" | "amber" | "rose" | "subtle" =
        c.churn_risk == null
            ? "subtle"
            : c.churn_risk >= 0.7
              ? "rose"
              : c.churn_risk >= 0.4
                ? "amber"
                : "emerald";
    return { sent, churnPct, churnTone };
}

export function OverviewHeader({
    c,
    compact = false,
}: {
    c: CustomerDetail;
    compact?: boolean;
}) {
    const { sent, churnPct, churnTone } = deriveScoreVisuals(c);
    return (
        <header className="rounded-lg border border-border bg-bg-card p-5">
            <div className="flex flex-wrap items-start justify-between gap-4">
                <div className="flex min-w-0 items-center gap-4">
                    <CustomerLogo
                        domain={c.domain}
                        name={c.name}
                        size={compact ? 40 : 56}
                    />
                    <div className="min-w-0">
                        <h1
                            className={`truncate font-bold ${
                                compact ? "text-xl" : "text-2xl"
                            }`}
                        >
                            {c.name}
                        </h1>
                        <div className="mt-1 flex flex-wrap gap-x-4 text-sm text-text-muted">
                            {c.domain ? (
                                <span>{c.domain}</span>
                            ) : (
                                <span className="italic text-text-subtle">
                                    no domain yet
                                </span>
                            )}
                            {c.industry ? <span>· {c.industry}</span> : null}
                            {c.timezone ? <span>· {c.timezone}</span> : null}
                        </div>
                    </div>
                </div>
                <div className="flex items-center gap-3">
                    <ScoreCard
                        label="Sentiment"
                        value={
                            c.sentiment_score != null
                                ? c.sentiment_score.toFixed(1)
                                : "—"
                        }
                        accentText={sent.text}
                        tone={sent.tone}
                    />
                    <ScoreCard
                        label="Churn risk"
                        value={churnPct}
                        tone={churnTone}
                    />
                    <ScoreCard
                        label="Multithreading"
                        value={`${c.multithreading_90d}`}
                        accentText="last 90d"
                        tone="subtle"
                    />
                </div>
            </div>

            {!compact && (
                <div className="mt-4 border-t border-border pt-4 text-sm">
                    <span className="text-text-subtle">Owners: </span>
                    {c.owners.length === 0 ? (
                        <span className="text-text-subtle">unassigned</span>
                    ) : (
                        c.owners.map((o, idx) => (
                            <span key={o.user_id}>
                                {idx > 0 ? ", " : ""}
                                <span className="font-medium">
                                    {o.name ?? o.email ?? "Unknown"}
                                </span>
                                <span className="ml-1 text-xs text-text-subtle">
                                    ({o.role})
                                </span>
                            </span>
                        ))
                    )}
                </div>
            )}
        </header>
    );
}

export function ContactsCard({ c }: { c: CustomerDetail }) {
    return (
        <section className="rounded-lg border border-border bg-bg-card">
            <div className="border-b border-border px-5 py-3">
                <h2 className="text-sm font-semibold">
                    Contacts ({c.contacts.length})
                </h2>
            </div>
            <div className="divide-y divide-border">
                {c.contacts.length === 0 ? (
                    <p className="px-5 py-4 text-sm text-text-subtle">
                        No contacts identified yet.
                    </p>
                ) : (
                    c.contacts.map((p) => <ContactRow key={p.id} c={p} />)
                )}
            </div>
        </section>
    );
}

export function ContactRow({ c }: { c: CustomerContactOut }) {
    const initials = (c.name || c.email || "?")
        .split(/\s+/)
        .filter(Boolean)
        .slice(0, 2)
        .map((t) => t[0]?.toUpperCase() ?? "")
        .join("");
    return (
        <div className="flex items-center gap-3 px-5 py-3">
            <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-bg-secondary text-xs font-medium text-text-muted">
                {initials}
            </div>
            <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-2">
                    <span className="truncate text-sm font-medium text-text">
                        {c.name ?? "Unnamed contact"}
                    </span>
                    {c.role ? (
                        <RoleChip role={c.role} confidence={c.role_confidence} />
                    ) : null}
                </div>
                {c.email || c.phone ? (
                    <div className="truncate text-xs text-text-subtle">
                        {c.email}
                        {c.email && c.phone ? " · " : ""}
                        {c.phone}
                    </div>
                ) : null}
            </div>
        </div>
    );
}

export function RoleChip({
    role,
    confidence,
}: {
    role: NonNullable<CustomerContactOut["role"]>;
    confidence: number | null;
}) {
    const label = contactRoleLabel(role);
    const isConfirmed = (confidence ?? 0) >= 0.8;
    return (
        <span
            title={
                isConfirmed
                    ? `${label} (confirmed)`
                    : `${label} (suggested — click the contact to confirm)`
            }
            className={
                isConfirmed
                    ? "rounded-full border border-primary/40 bg-primary-soft px-2 py-0.5 text-xs font-medium text-primary"
                    : "rounded-full border border-dashed border-text-subtle px-2 py-0.5 text-xs text-text-subtle"
            }
        >
            {label}
        </span>
    );
}

export function InteractionsCard({
    c,
    title = "Recent interactions",
    expandFirst = false,
}: {
    c: CustomerDetail;
    title?: string;
    expandFirst?: boolean;
}) {
    return (
        <section className="rounded-lg border border-border bg-bg-card">
            <div className="border-b border-border px-5 py-3">
                <h2 className="text-sm font-semibold">
                    {title} ({c.recent_interactions.length})
                </h2>
            </div>
            <div className="divide-y divide-border">
                {c.recent_interactions.length === 0 ? (
                    <p className="px-5 py-4 text-sm text-text-subtle">
                        No interactions yet.
                    </p>
                ) : (
                    c.recent_interactions.map((ix, idx) => (
                        <InteractionRow
                            key={ix.id}
                            ix={ix}
                            expanded={expandFirst && idx === 0}
                        />
                    ))
                )}
            </div>
        </section>
    );
}

export function InteractionRow({
    ix,
    expanded = false,
}: {
    ix: CustomerInteractionSummary;
    expanded?: boolean;
}) {
    return (
        <Link
            id={`interaction-${ix.id}`}
            href={`/interactions/${ix.id}`}
            className="block px-5 py-4 transition-colors hover:bg-bg-card-hover"
        >
            <div className="flex items-baseline justify-between gap-4">
                <div className="font-medium text-text">
                    {ix.title ?? `${ix.channel} interaction`}
                </div>
                <div className="shrink-0 text-xs text-text-subtle">
                    {formatRelative(ix.created_at)}
                </div>
            </div>
            {ix.summary_excerpt ? (
                <p
                    className={`mt-1 text-sm text-text-muted ${
                        expanded ? "" : "line-clamp-2"
                    }`}
                >
                    {ix.summary_excerpt}
                </p>
            ) : null}
            <div className="mt-2 flex flex-wrap gap-2 text-xs">
                <span className="rounded-full border border-border px-2 py-0.5 capitalize text-text-muted">
                    {ix.channel}
                </span>
                <span className="rounded-full border border-border px-2 py-0.5 capitalize text-text-muted">
                    {ix.status}
                </span>
                {ix.sentiment_score != null ? (
                    <span className="rounded-full border border-border px-2 py-0.5 text-text-muted">
                        Sent {ix.sentiment_score.toFixed(1)}
                    </span>
                ) : null}
            </div>
        </Link>
    );
}

export function ActionItemsCard({ c }: { c: CustomerDetail }) {
    return (
        <section className="rounded-lg border border-border bg-bg-card">
            <div className="border-b border-border px-5 py-3">
                <h2 className="text-sm font-semibold">
                    Open action items ({c.open_action_items.length})
                </h2>
            </div>
            <div className="divide-y divide-border">
                {c.open_action_items.length === 0 ? (
                    <p className="px-5 py-4 text-sm text-text-subtle">
                        No open action items for this customer.
                    </p>
                ) : (
                    c.open_action_items.map((ai) => (
                        <ActionItemRow key={ai.id} ai={ai} />
                    ))
                )}
            </div>
        </section>
    );
}

export function ActionItemRow({ ai }: { ai: CustomerActionItemSummary }) {
    return (
        <Link
            id={`action-${ai.id}`}
            href={`/interactions/${ai.interaction_id}`}
            className="block px-5 py-3 transition-colors hover:bg-bg-card-hover"
        >
            <div className="flex items-baseline justify-between gap-4">
                <div className="font-medium text-text">{ai.title}</div>
                <div className="shrink-0 text-xs uppercase tracking-wide text-text-subtle">
                    {ai.priority ?? ""}
                </div>
            </div>
            {ai.description ? (
                <p className="mt-1 line-clamp-2 text-sm text-text-muted">
                    {ai.description}
                </p>
            ) : null}
            {ai.category ? (
                <span className="mt-1 inline-block rounded-full border border-border px-2 py-0.5 text-xs text-text-subtle">
                    {ai.category}
                </span>
            ) : null}
        </Link>
    );
}

// ─── Phase 4: Deal Warnings ────────────────────────────────────────────

const SEV_RANK: Record<string, number> = { high: 0, medium: 1, low: 2 };

function severityChipClasses(sev: string): string {
    if (sev === "high")
        return "border-accent-rose/60 bg-accent-rose/10 text-accent-rose";
    if (sev === "medium")
        return "border-accent-amber/60 bg-accent-amber/10 text-accent-amber";
    return "border-border bg-bg-secondary text-text-muted";
}

export function WarningsCard({ c }: { c: CustomerDetail }) {
    const sorted = [...c.warnings].sort(
        (a, b) => (SEV_RANK[a.severity] ?? 3) - (SEV_RANK[b.severity] ?? 3),
    );
    return (
        <section className="rounded-lg border border-border bg-bg-card">
            <div className="flex items-baseline justify-between border-b border-border px-5 py-3">
                <h2 className="text-sm font-semibold">
                    Deal warnings ({sorted.length})
                </h2>
                <span className="text-[11px] text-text-subtle">
                    Linda’s open findings on this account
                </span>
            </div>
            {sorted.length === 0 ? (
                <p className="px-5 py-4 text-sm text-text-subtle">
                    No active warnings — nothing concerning surfaced on the
                    last few calls.
                </p>
            ) : (
                <ul className="divide-y divide-border">
                    {sorted.map((w) => (
                        <li key={w.id} className="px-5 py-3">
                            <WarningRow warning={w} customerId={c.id} />
                        </li>
                    ))}
                </ul>
            )}
        </section>
    );
}

function WarningRow({
    warning,
    customerId,
}: {
    warning: CustomerWarningOut;
    customerId: string;
}) {
    const [open, setOpen] = useState(false);
    const dismiss = useDismissWarning(customerId);
    const sevCls = severityChipClasses(warning.severity);
    return (
        <div>
            <button
                type="button"
                onClick={() => setOpen((v) => !v)}
                className="flex w-full items-center justify-between gap-3 text-left"
            >
                <span className="flex flex-wrap items-center gap-2">
                    <span
                        className={`rounded-full border px-2 py-0.5 text-xs font-medium ${sevCls}`}
                    >
                        {warningKindLabel(warning.kind, warning.label)}
                    </span>
                    <span className="text-[11px] uppercase tracking-wide text-text-subtle">
                        {warning.severity}
                    </span>
                    <span className="text-[11px] text-text-subtle">
                        first seen {formatRelative(warning.first_detected_at)}
                    </span>
                </span>
                <span className="text-xs text-text-subtle">
                    {open ? "Hide" : "Why?"}
                </span>
            </button>
            {open && (
                <div className="mt-2 rounded-md border border-border bg-bg-secondary px-3 py-2 text-sm">
                    {warning.evidence_text ? (
                        <p className="text-text-muted">
                            “{warning.evidence_text}”
                        </p>
                    ) : (
                        <p className="text-text-subtle">
                            No evidence excerpt was captured.
                        </p>
                    )}
                    <div className="mt-2 flex items-center justify-between gap-3">
                        {warning.evidence_interaction_id ? (
                            <Link
                                href={`/interactions/${warning.evidence_interaction_id}`}
                                className="text-xs text-primary hover:underline"
                            >
                                View source call →
                            </Link>
                        ) : (
                            <span />
                        )}
                        <button
                            type="button"
                            disabled={dismiss.isPending}
                            onClick={() => dismiss.mutate(warning.id)}
                            className="text-xs text-text-subtle hover:text-text"
                        >
                            {dismiss.isPending ? "Dismissing…" : "Dismiss"}
                        </button>
                    </div>
                </div>
            )}
        </div>
    );
}

// ─── Phase 4: Commitments (both-sides promises) ───────────────────────

export function CommitmentsCard({ c }: { c: CustomerDetail }) {
    const open = c.commitments.filter(
        (x) => x.status === "pending" || x.status === "overdue",
    );
    const done = c.commitments.filter((x) => x.status === "done").slice(0, 5);
    return (
        <section className="rounded-lg border border-border bg-bg-card">
            <div className="flex items-baseline justify-between border-b border-border px-5 py-3">
                <h2 className="text-sm font-semibold">
                    Commitments ({open.length} open)
                </h2>
                <span className="text-[11px] text-text-subtle">
                    Promises made on calls — both sides
                </span>
            </div>
            {open.length === 0 && done.length === 0 ? (
                <p className="px-5 py-4 text-sm text-text-subtle">
                    No commitments captured yet.
                </p>
            ) : (
                <ul className="divide-y divide-border">
                    {open.map((cm) => (
                        <li key={cm.id} className="px-5 py-3">
                            <CommitmentRow c={cm} customerId={c.id} />
                        </li>
                    ))}
                    {done.map((cm) => (
                        <li
                            key={cm.id}
                            className="px-5 py-3 opacity-70"
                        >
                            <CommitmentRow c={cm} customerId={c.id} />
                        </li>
                    ))}
                </ul>
            )}
        </section>
    );
}

function CommitmentRow({
    c,
    customerId,
}: {
    c: CommitmentOut;
    customerId: string;
}) {
    const update = useUpdateCommitment(customerId);
    const isCustomerSide = c.actor_side === "customer";
    const actorName =
        c.actor_user_name ||
        c.actor_contact_name ||
        (isCustomerSide ? "Customer" : "Rep");
    const initials = (actorName || "?")
        .split(/\s+/)
        .filter(Boolean)
        .slice(0, 2)
        .map((t) => t[0]?.toUpperCase() ?? "")
        .join("");
    const overdue =
        c.status === "pending" &&
        c.due_date &&
        new Date(c.due_date).getTime() < Date.now();
    return (
        <div className="flex items-start gap-3">
            <div
                className={
                    isCustomerSide
                        ? "flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-accent-emerald/15 text-xs font-medium text-accent-emerald"
                        : "flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-primary-soft text-xs font-medium text-primary"
                }
                title={isCustomerSide ? "Customer-side promise" : "Rep-side promise"}
            >
                {initials || "·"}
            </div>
            <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-baseline gap-2">
                    <span className="text-sm font-medium text-text">
                        {actorName}
                    </span>
                    <span className="text-[11px] uppercase tracking-wide text-text-subtle">
                        {isCustomerSide ? "customer" : "rep"}
                    </span>
                    {c.status === "done" ? (
                        <span className="rounded-full border border-accent-emerald/50 bg-accent-emerald/10 px-2 py-0.5 text-[11px] text-accent-emerald">
                            done
                        </span>
                    ) : overdue ? (
                        <span className="rounded-full border border-accent-rose/50 bg-accent-rose/10 px-2 py-0.5 text-[11px] text-accent-rose">
                            overdue
                        </span>
                    ) : null}
                </div>
                <p className="mt-0.5 text-sm text-text-muted">{c.text}</p>
                <div className="mt-1 flex flex-wrap items-center gap-3 text-[11px] text-text-subtle">
                    {c.due_date ? (
                        <span>due {formatRelative(c.due_date)}</span>
                    ) : (
                        <span>no due date</span>
                    )}
                    <Link
                        href={`/interactions/${c.interaction_id}`}
                        className="text-primary hover:underline"
                    >
                        source call
                    </Link>
                    {c.status === "pending" && !isCustomerSide ? (
                        <button
                            type="button"
                            disabled={update.isPending}
                            onClick={() =>
                                update.mutate({
                                    commitmentId: c.id,
                                    status: "done",
                                })
                            }
                            className="text-text-subtle hover:text-text"
                        >
                            {update.isPending ? "…" : "Mark done"}
                        </button>
                    ) : null}
                    {c.status === "done" ? (
                        <button
                            type="button"
                            disabled={update.isPending}
                            onClick={() =>
                                update.mutate({
                                    commitmentId: c.id,
                                    status: "pending",
                                })
                            }
                            className="text-text-subtle hover:text-text"
                        >
                            {update.isPending ? "…" : "Reopen"}
                        </button>
                    ) : null}
                </div>
            </div>
        </div>
    );
}
