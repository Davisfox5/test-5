"use client";

/**
 * Upsell Opportunities tab.
 *
 * Surfaces the customers showing the strongest buying signals on their
 * most recent call. Pulls from /analytics/account-health.upsell —
 * each row's score is the customer's last-call upsell_score (0–1) from
 * Linda's analysis.
 */

import Link from "next/link";
import {
    useAccountHealth,
    type AccountHealthRow,
} from "@/lib/analytics";
import { formatRelative } from "@/lib/interactions";

export function UpsellOpportunitiesView() {
    const accounts = useAccountHealth(30, 20);
    const rows = accounts.data?.upsell ?? [];

    return (
        <div className="space-y-6">
            <section className="rounded-lg border border-border bg-bg-card">
                <header className="border-b border-border px-5 py-3">
                    <h3 className="text-sm font-semibold">
                        Accounts showing buying signals
                    </h3>
                    <p className="mt-0.5 text-xs text-text-subtle">
                        Sorted by upsell score on each customer&apos;s most
                        recent analyzed call.
                    </p>
                </header>
                {accounts.isLoading ? (
                    <SkeletonRows />
                ) : accounts.error ? (
                    <p className="px-5 py-6 text-sm text-accent-rose">
                        Couldn&apos;t load opportunities.
                    </p>
                ) : rows.length === 0 ? (
                    <p className="px-5 py-10 text-center text-sm text-text-muted">
                        No upsell signals detected yet. Once Linda hears
                        expansion language. extra seats, new use cases,
                        budget commentary. those accounts will surface
                        here.
                    </p>
                ) : (
                    <ul className="divide-y divide-border">
                        {rows.map((r) => (
                            <UpsellRow key={r.customer_id} row={r} />
                        ))}
                    </ul>
                )}
            </section>

            <section className="grid grid-cols-1 gap-4 md:grid-cols-2">
                <SignalCard
                    title="What we mean by 'upsell signal'"
                    body={[
                        "Linda scores every analyzed call on a 0-to-1 upsell scale. A 0.5+ shows up here. Strong signals: asking about pricing for more seats, mentioning new teams or use cases internally, requesting features adjacent to a paid tier, or a champion saying 'I'd love to roll this out to my whole org.'",
                        "Score reflects the MOST RECENT call. A live signal beats a 90-day-old conversation. interest decays fast.",
                    ]}
                />
                <SignalCard
                    title="How the score is calculated"
                    body={[
                        "Six signals weighted into one: explicit expansion asks, mention of additional teams or stakeholders, budget/timing language, feature requests aligning with paid tiers, positive sentiment paired with operational urgency, and competitor displacement narratives.",
                        "We weight EXPLICIT asks more than implicit cues. 'send me a quote for 50 seats' is worth more than 'this is great.' But we do reward consistency: three positive calls in a row outscore one enthusiastic mention.",
                    ]}
                />
                <SignalCard
                    title="Upsell plays. what to actually do"
                    body={[
                        "Strike while warm: send the quote or proposal within 48 hours of the flagged call. Anything later and you're re-creating the moment from scratch.",
                        "Anchor to their words: pull the exact line from the call ('you said you wanted X for Y team') and lead the follow-up email with it. Generic upsell emails fail.",
                        "Multi-thread before pitching: identify the economic buyer (often not the champion who spoke) and get them on a 20-minute scope call before sending pricing.",
                        "Offer a no-risk expansion pilot for scores > 0.8. 30 days of 5 extra seats at no charge, then convert. The reduced friction converts far better than a hard close.",
                    ]}
                />
                <SignalCard
                    title="What this list doesn't catch yet"
                    body={[
                        "Silent expansion. customers who'd buy if asked, but never bring it up themselves. The 'Stale accounts' panel on the dashboard is a different signal for those.",
                        "Cross-sell across product lines. we only score expansion of the existing relationship, not lateral product fit. That requires product taxonomy we don't have yet.",
                        "Contract-renewal timing. a strong signal 60 days before renewal is more valuable than the same signal a week after a fresh contract. Surface that context yourself for now.",
                    ]}
                />
            </section>
        </div>
    );
}

function UpsellRow({ row }: { row: AccountHealthRow }) {
    const score = row.score ?? 0;
    const band =
        score >= 0.8 ? "hot" : score >= 0.6 ? "warm" : "interested";
    const tone =
        band === "hot"
            ? "text-accent-emerald"
            : band === "warm"
              ? "text-accent-emerald/80"
              : "text-accent-cyan";

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
