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
import { useParams } from "next/navigation";
import { useState } from "react";
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
    const id = params?.id;
    const detail = useCustomerDetail(id);
    const [tab, setTab] = useState<TabKey>("signals");

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
}: {
    churnRisk: number | null;
    upsellScore: number | null;
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
                />
                <SignalLight label="Upsell opportunity" score={upsellScore} />
            </div>
        </section>
    );
}

function SignalLight({
    label,
    score,
    invertColors,
}: {
    label: string;
    score: number | null;
    invertColors?: boolean;
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
        </div>
    );
}

function bucketFor(score: number): "high" | "medium" | "low" {
    if (score >= 0.7) return "high";
    if (score >= 0.4) return "medium";
    return "low";
}
