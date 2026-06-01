"use client";

/**
 * CS account drill-down.
 *
 * Renders the per-customer health breakdown (the five weighted
 * components from the score) plus controls to set the renewal date,
 * change onboarding status, and trigger a fresh recompute.
 */

import Link from "next/link";
import { useParams } from "next/navigation";
import {
    useAccountHealth,
    usePatchCustomerCsFields,
    ONBOARDING_LABEL,
    riskBand,
    type OnboardingStatus,
} from "@/lib/cs";

const ONBOARDING_OPTIONS: OnboardingStatus[] = [
    "not_started",
    "in_progress",
    "stalled",
    "completed",
];

export default function CSAccountPage() {
    const params = useParams();
    const id = typeof params.id === "string" ? params.id : params.id?.[0] || null;
    const { data: detail, isLoading, refetch } = useAccountHealth(id);
    const patch = usePatchCustomerCsFields();

    if (!id || isLoading) return <p className="text-text-muted">Loading…</p>;
    if (!detail) {
        return (
            <div className="rounded-lg border border-border bg-bg-card p-4">
                <p className="text-text">Account not found.</p>
                <Link href="/cs" className="mt-2 inline-block text-sm underline">
                    Back to CS
                </Link>
            </div>
        );
    }

    const band = riskBand(detail.renewal_risk_score);
    const riskClass =
        band === "high"
            ? "bg-error-soft text-error border-error"
            : band === "medium"
            ? "bg-amber-100 text-amber-700 border-amber-300"
            : "bg-emerald-50 text-emerald-700 border-emerald-300";

    return (
        <div className="space-y-6">
            <header className="space-y-1">
                <Link
                    href="/cs"
                    className="text-xs text-text-muted hover:underline"
                >
                    ← Back to CS
                </Link>
                <div className="flex flex-wrap items-baseline gap-3">
                    <h1 className="text-2xl font-bold">{detail.customer_name}</h1>
                    <span
                        className={`rounded border px-2 py-0.5 text-[10px] font-semibold uppercase ${riskClass}`}
                    >
                        Risk: {band} · {detail.renewal_risk_score.toFixed(0)}
                    </span>
                </div>
            </header>

            <section className="grid gap-4 md:grid-cols-2">
                <div className="rounded-lg border border-border bg-bg-card p-4">
                    <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-text-muted">
                        Health breakdown
                    </h2>
                    <div className="space-y-2 text-sm">
                        <HealthBar
                            label="Engagement"
                            value={detail.health_breakdown.engagement}
                        />
                        <HealthBar
                            label="Sentiment"
                            value={detail.health_breakdown.sentiment}
                        />
                        <HealthBar
                            label="Churn signal"
                            value={detail.health_breakdown.churn_signal}
                        />
                        <HealthBar
                            label="Onboarding"
                            value={detail.health_breakdown.onboarding}
                        />
                        <HealthBar
                            label="Renewal proximity"
                            value={detail.health_breakdown.renewal_proximity}
                        />
                    </div>
                    <p className="mt-3 text-sm">
                        Overall score:{" "}
                        <strong className="text-text">
                            {detail.health_breakdown.overall.toFixed(1)} / 100
                        </strong>
                        {detail.health_score !== null && (
                            <span className="ml-2 text-xs text-text-subtle">
                                (last persisted: {detail.health_score.toFixed(1)})
                            </span>
                        )}
                    </p>
                    <p className="mt-1 text-xs text-text-muted">
                        Based on {detail.health_breakdown.cs_interaction_count}{" "}
                        CS interactions in the last 90 days
                        {detail.health_breakdown.last_cs_at
                            ? `, most recent ${new Date(detail.health_breakdown.last_cs_at).toLocaleDateString()}`
                            : ""}
                        .
                    </p>
                    <button
                        onClick={() => refetch()}
                        className="mt-3 rounded border border-border bg-bg-card px-3 py-1 text-xs hover:bg-bg-card-hover"
                    >
                        Refresh
                    </button>
                </div>

                <div className="rounded-lg border border-border bg-bg-card p-4 space-y-3">
                    <h2 className="text-sm font-semibold uppercase tracking-wide text-text-muted">
                        Account controls
                    </h2>
                    <label className="block text-xs">
                        Renewal date
                        <input
                            type="date"
                            defaultValue={detail.renewal_date ?? ""}
                            onChange={(e) =>
                                patch.mutate({
                                    customerId: id,
                                    patch: { renewal_date: e.target.value || null },
                                })
                            }
                            className="mt-1 w-full rounded border border-border bg-bg-card px-2 py-1 text-sm"
                        />
                    </label>
                    <label className="block text-xs">
                        Onboarding status
                        <select
                            value={detail.onboarding_status ?? ""}
                            onChange={(e) =>
                                patch.mutate({
                                    customerId: id,
                                    patch: {
                                        onboarding_status:
                                            e.target.value as OnboardingStatus,
                                    },
                                })
                            }
                            className="mt-1 w-full rounded border border-border bg-bg-card px-2 py-1 text-sm"
                        >
                            <option value="">Not set</option>
                            {ONBOARDING_OPTIONS.map((o) => (
                                <option key={o} value={o}>
                                    {ONBOARDING_LABEL[o]}
                                </option>
                            ))}
                        </select>
                    </label>
                </div>
            </section>
        </div>
    );
}

function HealthBar({ label, value }: { label: string; value: number }) {
    const pct = Math.max(0, Math.min(100, value));
    return (
        <div>
            <div className="flex justify-between text-xs">
                <span className="text-text-muted">{label}</span>
                <span className="text-text">{value.toFixed(0)}</span>
            </div>
            <div className="mt-0.5 h-1.5 w-full overflow-hidden rounded bg-bg">
                <div
                    className="h-full bg-primary"
                    style={{ width: `${pct}%` }}
                />
            </div>
        </div>
    );
}
