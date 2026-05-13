"use client";

/**
 * Customer detail page.
 *
 * Tabbed structure: Signals / Interactions / Action items / Notes.
 * Replaces the four "Layout 1-4" experimental layouts that were a
 * pre-launch evaluation tool. The OverviewHeader is always shown at
 * the top; tabs swap the body underneath.
 *
 * One detail fetch feeds every tab — they all consume CustomerDetail.
 */

import Link from "next/link";
import { useParams, useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";
import { useCustomerDetail } from "@/lib/customers";
import { CustomerBehaviorSignals } from "@/components/customer-signals/customer-behavior-signals";
import {
    ActionItemsCard,
    CommitmentsCard,
    ContactsCard,
    InteractionsCard,
    OverviewHeader,
    WarningsCard,
} from "./_layouts/shared";

type TabKey = "signals" | "interactions" | "action_items" | "notes";

const TABS: Array<{ key: TabKey; label: string }> = [
    { key: "signals", label: "Signals" },
    { key: "interactions", label: "Interactions" },
    { key: "action_items", label: "Action items" },
    { key: "notes", label: "Notes" },
];

export default function CustomerDetailPage() {
    const params = useParams<{ id: string }>();
    const searchParams = useSearchParams();
    const id = params?.id;
    const detail = useCustomerDetail(id);

    // Deep-link support — the dashboard's Recent Calls + Open Action
    // Items rows link here with ``?tab=interactions&focus=interaction-
    // <id>`` (or ``?tab=action_items&focus=action-<id>``). The tab
    // param picks the right pane; the focus param is consumed by an
    // effect below that scrolls the matching DOM node into view and
    // applies a brief highlight class.
    const initialTab = ((): TabKey => {
        const t = searchParams?.get("tab");
        if (t === "interactions" || t === "action_items" || t === "notes" || t === "signals") {
            return t as TabKey;
        }
        return "signals";
    })();
    const [tab, setTab] = useState<TabKey>(initialTab);
    const focus = searchParams?.get("focus") ?? null;

    useEffect(() => {
        if (!focus || detail.isLoading) return;
        // Give the tab pane a tick to render before we look for the node.
        const tid = setTimeout(() => {
            const el = document.getElementById(focus);
            if (!el) return;
            el.scrollIntoView({ behavior: "smooth", block: "center" });
            // Brief outline highlight so the user sees which row we landed on.
            el.classList.add("ring-2", "ring-primary");
            setTimeout(() => {
                el.classList.remove("ring-2", "ring-primary");
            }, 2400);
        }, 80);
        return () => clearTimeout(tid);
    }, [focus, tab, detail.isLoading]);

    if (!id) return null;

    if (detail.isLoading) {
        return (
            <div className="space-y-4">
                <div className="h-8 w-1/3 animate-pulse rounded bg-bg-card-hover" />
                <div className="h-4 w-1/2 animate-pulse rounded bg-bg-card-hover" />
                <div className="h-64 animate-pulse rounded-lg bg-bg-card" />
            </div>
        );
    }

    if (detail.error || !detail.data) {
        return (
            <div className="space-y-3">
                <Link
                    href="/customers"
                    className="text-sm text-primary hover:underline"
                >
                    ← Back to customers
                </Link>
                <p className="text-accent-rose">
                    Couldn&apos;t load this customer.
                </p>
            </div>
        );
    }

    const c = detail.data;

    return (
        <div className="space-y-6">
            <Link
                href="/customers"
                className="text-sm text-primary hover:underline"
            >
                ← Back to customers
            </Link>

            <OverviewHeader c={c} />

            <div
                role="tablist"
                aria-label="Customer detail tabs"
                className="flex items-center gap-1 border-b border-border"
            >
                {TABS.map((t) => (
                    <button
                        key={t.key}
                        type="button"
                        role="tab"
                        aria-selected={tab === t.key}
                        onClick={() => setTab(t.key)}
                        className={`-mb-px border-b-2 px-3 py-2 text-sm font-medium transition-colors ${
                            tab === t.key
                                ? "border-primary text-text"
                                : "border-transparent text-text-muted hover:text-text"
                        }`}
                    >
                        {t.label}
                    </button>
                ))}
            </div>

            {tab === "signals" && (
                <div className="space-y-6">
                    <WarningsCard c={c} />
                    <CustomerBehaviorSignals customerId={c.id} />
                    <ChurnUpsellPairedBlock
                        churnRisk={c.churn_risk}
                        upsellScore={c.upsell_score}
                        customerId={c.id}
                    />
                    <ContactsCard c={c} />
                </div>
            )}
            {tab === "interactions" && <InteractionsCard c={c} />}
            {tab === "action_items" && (
                <div className="space-y-6">
                    <ActionItemsCard c={c} />
                    <CommitmentsCard c={c} />
                </div>
            )}
            {tab === "notes" && (
                <section className="rounded-lg border border-border bg-bg-card p-5">
                    <h3 className="text-sm font-semibold">Notes</h3>
                    <p className="mt-2 text-sm text-text-muted">
                        Customer notes inbox is wired through the existing
                        customer-notes API; the tab is reserved for that
                        component to slot in here.
                    </p>
                </section>
            )}
        </div>
    );
}

// ── Paired churn / upsell block ─────────────────────────────────────────

function ChurnUpsellPairedBlock({
    churnRisk,
    upsellScore,
    customerId,
}: {
    churnRisk: number | null;
    upsellScore: number | null;
    customerId: string;
}) {
    if (churnRisk == null && upsellScore == null) return null;
    return (
        <section className="rounded-lg border border-border bg-bg-card p-5">
            <h3 className="mb-3 text-sm font-semibold">Risk + opportunity</h3>
            <div className="grid gap-4 sm:grid-cols-2">
                <SignalLight
                    label="Churn risk"
                    score={churnRisk}
                    invertColors
                    customerId={customerId}
                    cta={{
                        label: "Draft a save play",
                        href: `/action-items?new=1&customer_id=${customerId}&category=churn_save`,
                    }}
                />
                <SignalLight
                    label="Upsell opportunity"
                    score={upsellScore}
                    customerId={customerId}
                    cta={{
                        label: "Draft an upsell pitch",
                        href: `/action-items?new=1&customer_id=${customerId}&category=upsell_pitch`,
                    }}
                />
            </div>
        </section>
    );
}

function SignalLight({
    label,
    score,
    invertColors,
    customerId,
    cta,
}: {
    label: string;
    score: number | null;
    invertColors?: boolean;
    customerId?: string;
    cta?: { label: string; href: string };
}) {
    if (score == null) {
        return (
            <div className="rounded-md border border-border-light bg-bg-secondary p-3 text-sm text-text-muted">
                <div className="font-medium text-text">{label}</div>
                <div className="mt-1 text-xs">No signal yet.</div>
            </div>
        );
    }
    const bucket = bucketFor(score);
    const colorMap = invertColors
        ? {
              high: "var(--accent-rose)",
              medium: "var(--accent-amber)",
              low: "var(--accent-emerald)",
          }
        : {
              high: "var(--accent-emerald)",
              medium: "var(--accent-amber)",
              low: "var(--text-subtle)",
          };
    const color = colorMap[bucket];
    // Only surface the CTA when the bucket warrants action — i.e. when
    // the signal is "live" (high/medium for churn, high/medium for
    // upsell). Low+inverted (= low churn) is already a positive signal
    // and doesn't need a button.
    const showCta =
        cta != null &&
        (invertColors ? bucket !== "low" : bucket !== "low") &&
        customerId != null;
    return (
        <div className="rounded-md border border-border-light bg-bg-secondary p-3 text-sm">
            <div className="flex items-center gap-2">
                <span
                    aria-hidden
                    className="inline-block h-3 w-3 rounded-full"
                    style={{ backgroundColor: color }}
                />
                <span className="font-medium text-text">{label}</span>
                <span className="ml-auto text-xs uppercase tracking-wide text-text-subtle">
                    {bucket}
                </span>
            </div>
            <div className="mt-1 text-xs text-text-muted">
                {(score * 100).toFixed(0)}% — based on the most recent analyzed
                interaction
            </div>
            {showCta && cta ? (
                <Link
                    href={cta.href}
                    className="mt-2 inline-block rounded border border-border bg-bg-card px-2 py-1 text-xs text-primary hover:bg-card-hover"
                >
                    {cta.label} →
                </Link>
            ) : null}
        </div>
    );
}

function bucketFor(score: number): "high" | "medium" | "low" {
    if (score >= 0.7) return "high";
    if (score >= 0.4) return "medium";
    return "low";
}
