"use client";

/**
 * At-Risk Accounts tab.
 *
 * Surfaces the customers showing the strongest churn signals on their
 * most recent call. Pulls from /analytics/account-health.at_risk —
 * each row's score is the customer's last-call churn_risk (0–1) from
 * Linda's analysis.
 *
 * Also explains, in plain language, what the signal means, how it's
 * calculated, and the standard save plays a CSM/AE can run.
 */

import Link from "next/link";
import {
    useAccountHealth,
    type AccountHealthRow,
} from "@/lib/analytics";
import { formatRelative } from "@/lib/interactions";

export function AtRiskView() {
    const accounts = useAccountHealth(30, 20);
    const rows = accounts.data?.at_risk ?? [];

    return (
        <div className="space-y-6">
            <section className="rounded-lg border border-border bg-bg-card">
                <header className="border-b border-border px-5 py-3">
                    <h3 className="text-sm font-semibold">
                        Accounts at risk of churning
                    </h3>
                    <p className="mt-0.5 text-xs text-text-subtle">
                        Sorted by churn risk on each customer&apos;s most
                        recent analyzed call.
                    </p>
                </header>
                {accounts.isLoading ? (
                    <SkeletonRows />
                ) : accounts.error ? (
                    <p className="px-5 py-6 text-sm text-accent-rose">
                        Couldn&apos;t load at-risk accounts.
                    </p>
                ) : rows.length === 0 ? (
                    <p className="px-5 py-10 text-center text-sm text-text-muted">
                        No churn signals detected in your tenant yet. Once
                        Linda hears cancellation language, frustration, or
                        warning signs on a call, those accounts will show up
                        here.
                    </p>
                ) : (
                    <ul className="divide-y divide-border">
                        {rows.map((r) => (
                            <AtRiskRow key={r.customer_id} row={r} />
                        ))}
                    </ul>
                )}
            </section>

            {/* Explainer + save plays. collapsed to two side-by-side cards
                so the page reads as a workspace, not just a list. */}
            <section className="grid grid-cols-1 gap-4 md:grid-cols-2">
                <SignalCard
                    title="What we mean by 'churn risk'"
                    body={[
                        "Linda scores every analyzed call on a 0-to-1 churn-risk scale. A 0.7+ is what we surface here. strong signals: cancellation language, frustration that wasn't resolved, repeated complaints, missed commitments by your team, or competitor name-drops paired with negative sentiment.",
                        "The number you see on each row is the score from the customer's MOST RECENT call. We use most-recent (not an average) because a single late-stage warning matters more than ten neutral check-ins.",
                    ]}
                />
                <SignalCard
                    title="How the score is calculated"
                    body={[
                        "Claude reads the full transcript and assesses six signals. cancellation language, unresolved objections, repeated complaints, competitor mentions with negative tone, missed commitments by your team, and overall sentiment trajectory. Each is weighted and combined into a single 0-to-1 score.",
                        "Sentiment alone isn't enough. a customer can sound polite while saying 'we're evaluating other options.' The scorer is trained to weight the WORDS more than the TONE for this metric.",
                    ]}
                />
                <SignalCard
                    title="Save plays. what to actually do"
                    body={[
                        "Same-day reach-out: a 10-minute call from the AE or CSM acknowledging the issue, within 24 hours of the flagged call. Don't wait until your next QBR.",
                        "Identify the WHO: was the warning from your champion or a blocker? A champion going quiet is more urgent than a known blocker venting.",
                        "Offer a 30-day usage review with concrete tied-to-pain metrics. not a pitch. The save play is to PROVE value, not re-sell.",
                        "Escalate to your manager if score > 0.85. those are renewal-at-risk calls and usually need a senior conversation.",
                    ]}
                />
                <SignalCard
                    title="What this list doesn't catch yet"
                    body={[
                        "Customers who have gone QUIET. no calls in 30+ days. See the 'Stale accounts' panel on the dashboard for those; silence is its own kind of churn signal.",
                        "Email-only churn signals. we score them, but conversations carry richer cues than written notes, so phone/video calls dominate this list.",
                        "Multi-stakeholder dynamics. if your champion is happy but their CFO is hostile, the AVERAGE score is misleading. Open the customer profile and read the most recent calls directly.",
                    ]}
                />
            </section>
        </div>
    );
}

function AtRiskRow({ row }: { row: AccountHealthRow }) {
    const score = row.score ?? 0;
    const band =
        score >= 0.85 ? "critical" : score >= 0.7 ? "high" : "medium";
    const tone =
        band === "critical"
            ? "text-accent-rose"
            : band === "high"
              ? "text-accent-rose/80"
              : "text-accent-amber";

    return (
        <li>
            <Link
                href={`/customers/${row.customer_id}`}
                className="flex items-center justify-between gap-3 px-5 py-3 hover:bg-bg-card-hover"
            >
                <div className="min-w-0 flex-1">
                    <div className="truncate text-sm font-medium">
                        {row.name}
                    </div>
                    <div className="mt-0.5 text-xs text-text-subtle">
                        Last contact:{" "}
                        {row.last_touch_at
                            ? formatRelative(row.last_touch_at)
                            : "-"}
                    </div>
                </div>
                <div className={`text-right ${tone}`}>
                    <div className="text-lg font-semibold">
                        {score.toFixed(2)}
                    </div>
                    <div className="text-[10px] uppercase tracking-wide">
                        {band}
                    </div>
                </div>
            </Link>
        </li>
    );
}

function SignalCard({
    title,
    body,
}: {
    title: string;
    body: string[];
}) {
    return (
        <div className="rounded-lg border border-border bg-bg-card p-5">
            <h4 className="text-sm font-semibold">{title}</h4>
            <div className="mt-2 space-y-2 text-sm text-text-muted">
                {body.map((p, i) => (
                    <p key={i}>{p}</p>
                ))}
            </div>
        </div>
    );
}

function SkeletonRows() {
    return (
        <ul className="divide-y divide-border">
            {Array.from({ length: 4 }).map((_, i) => (
                <li
                    key={i}
                    className="flex items-center justify-between gap-3 px-5 py-3"
                >
                    <div className="flex-1 animate-pulse space-y-2">
                        <div className="h-3 w-1/2 rounded bg-bg-card-hover" />
                        <div className="h-2 w-1/4 rounded bg-bg-card-hover" />
                    </div>
                </li>
            ))}
        </ul>
    );
}
