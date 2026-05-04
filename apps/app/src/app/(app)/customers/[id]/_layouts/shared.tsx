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
import {
    contactRoleLabel,
    faviconFor,
    type CustomerActionItemSummary,
    type CustomerContactOut,
    type CustomerDetail,
    type CustomerInteractionSummary,
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
            href={`/interactions/${ix.id}`}
            className="block px-5 py-4 hover:bg-bg-card-hover"
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
            href={`/interactions/${ai.interaction_id}`}
            className="block px-5 py-3 hover:bg-bg-card-hover"
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
