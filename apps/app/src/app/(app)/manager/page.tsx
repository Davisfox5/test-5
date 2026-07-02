"use client";

/**
 * Manager — multi-motion 10,000-foot view.
 *
 * Reads the signed-in user's ``manager_domains`` and renders one tab
 * per motion they oversee (Sales / Customer Success / IT Support).
 * When they hold 2+ scopes, an extra "Journey" tab appears with the
 * cross-motion handoff and at-risk-account view. Single-scope managers
 * see the existing layout for their one motion.
 *
 * This page does zero LLM work — narratives, alerts, and
 * recommendations are precomputed by the backend's daily refresh +
 * 15-minute anomaly scan. The Apply button on a recommendation row is
 * the only mutation here that produces a new artifact.
 */

import { useMemo, useState } from "react";
import {
    useAcknowledgeAlert,
    useApplyRecommendation,
    useDismissAlert,
    useDismissRecommendation,
    useJourney,
    useManagerAlerts,
    useManagerNarrative,
    useManagerRecommendations,
    useRefreshNarrative,
    useTrainingGap,
    type AlertKind,
    type JourneyAccountRow,
    type JourneyHandoffRow,
    type ManagerAlert,
    type ManagerRecommendation,
    type Severity,
    ALERT_KIND_LABEL,
    CATEGORY_LABEL,
    DOMAIN_LABEL,
} from "@/lib/manager";
import { useMe, type Domain } from "@/lib/me";

const SEVERITY_BADGE: Record<Severity, string> = {
    high: "bg-error-soft text-error border-error",
    medium: "bg-amber-100 text-amber-700 border-amber-300",
    low: "bg-blue-50 text-blue-700 border-blue-200",
};

type TabKey = Domain | "journey";

const ORDERED_DOMAINS: Domain[] = [
    "sales",
    "customer_service",
    "it_support",
    "generic",
];

export default function ManagerPage() {
    const me = useMe();
    const managerDomains = (me.data?.user?.manager_domains || []) as Domain[];
    const isTenantAdmin = me.data?.user?.is_tenant_admin ?? false;

    // Multi-motion shell (tabs + Journey) is tenant-admin-only. Most
    // managers oversee one motion; for the rare multi-scope manager,
    // a small selector at the top of the page lets them switch which
    // motion's dashboard they're looking at without exposing the
    // cross-section. Admins get the full tabbed view + Journey.
    const managerOwnDomains = useMemo<Domain[]>(
        () => ORDERED_DOMAINS.filter((d) => managerDomains.includes(d)),
        [managerDomains],
    );
    const adminAllDomains: Domain[] = [
        "sales",
        "customer_service",
        "it_support",
    ];

    if (me.isLoading) {
        return <p className="text-text-muted">Loading…</p>;
    }
    if (!me.data?.user) {
        return <p className="text-text-muted">Sign in to view manager dashboards.</p>;
    }
    if (!isTenantAdmin && managerOwnDomains.length === 0) {
        return (
            <div className="rounded-lg border border-border bg-bg-card p-4">
                <p className="text-text">
                    You don't have manager access to any motions yet.
                </p>
                <p className="mt-1 text-sm text-text-muted">
                    Ask your tenant admin to grant you manager scope for Sales,
                    Customer Success, or IT Support under Settings &rarr; User
                    Management.
                </p>
            </div>
        );
    }

    if (isTenantAdmin) {
        return <AdminShell domains={adminAllDomains} />;
    }
    return <ManagerShell domains={managerOwnDomains} />;
}

// ─────────────────────────────────────────────────────────────────────
// Manager shell — single motion at a time, with a discrete picker
// when the user happens to manage more than one. No Journey tab.
// ─────────────────────────────────────────────────────────────────────

function ManagerShell({ domains }: { domains: Domain[] }) {
    const [selected, setSelected] = useState<Domain>(domains[0]);
    // If a user's scope grant changes mid-session, drop back to the
    // first valid motion rather than rendering an empty view.
    const active = domains.includes(selected) ? selected : domains[0];

    return (
        <div className="space-y-6">
            <div className="flex flex-wrap items-baseline justify-between gap-3">
                <h1 className="text-2xl font-bold">Manager</h1>
                {domains.length > 1 ? (
                    <label className="text-sm text-text-muted">
                        Viewing:{" "}
                        <select
                            value={active}
                            onChange={(e) =>
                                setSelected(e.target.value as Domain)
                            }
                            className="ml-1 rounded border border-border bg-bg-card px-2 py-1 text-sm text-text"
                        >
                            {domains.map((d) => (
                                <option key={d} value={d}>
                                    {DOMAIN_LABEL[d]}
                                </option>
                            ))}
                        </select>
                    </label>
                ) : null}
            </div>
            <MotionView domain={active} />
        </div>
    );
}

// ─────────────────────────────────────────────────────────────────────
// Admin shell — tabs across every motion plus the Journey tab.
// Reserved for ``is_tenant_admin=true`` because the multi-tab UI was
// adding more confusion than utility for plain multi-scope managers.
// ─────────────────────────────────────────────────────────────────────

function AdminShell({ domains }: { domains: Domain[] }) {
    const [tab, setTab] = useState<TabKey>(domains[0] || "sales");

    return (
        <div className="space-y-6">
            <div className="flex items-baseline justify-between gap-4">
                <h1 className="text-2xl font-bold">Manager</h1>
                <span className="text-xs text-text-subtle">
                    Tenant admin view
                </span>
            </div>

            <div className="flex flex-wrap gap-1 border-b border-border">
                {domains.map((d) => (
                    <TabButton
                        key={d}
                        label={DOMAIN_LABEL[d]}
                        active={tab === d}
                        onClick={() => setTab(d)}
                    />
                ))}
                <TabButton
                    label="Journey"
                    active={tab === "journey"}
                    onClick={() => setTab("journey")}
                />
            </div>

            {tab === "journey" ? (
                <JourneyView />
            ) : (
                <MotionView domain={tab as Domain} />
            )}
        </div>
    );
}

function TabButton({
    label,
    active,
    onClick,
}: {
    label: string;
    active: boolean;
    onClick: () => void;
}) {
    return (
        <button
            onClick={onClick}
            className={
                "rounded-t-md px-4 py-2 text-sm font-medium transition " +
                (active
                    ? "border-b-2 border-primary text-text"
                    : "text-text-muted hover:text-text")
            }
        >
            {label}
        </button>
    );
}

// ─────────────────────────────────────────────────────────────────────
// Per-motion view (one tab)
// ─────────────────────────────────────────────────────────────────────

function MotionView({ domain }: { domain: Domain }) {
    const [windowDays, setWindowDays] = useState(30);
    return (
        <div className="space-y-6">
            <div className="flex items-baseline justify-between gap-2">
                <p className="text-sm text-text-muted">
                    Manager view for {DOMAIN_LABEL[domain].toLowerCase()}.
                </p>
                <label className="text-sm text-text-muted">
                    Training-gap window:{" "}
                    <select
                        value={windowDays}
                        onChange={(e) =>
                            setWindowDays(parseInt(e.target.value, 10))
                        }
                        className="ml-1 rounded border border-border bg-bg-card px-2 py-1 text-sm text-text"
                    >
                        <option value={7}>Last 7 days</option>
                        <option value={30}>Last 30 days</option>
                        <option value={90}>Last 90 days</option>
                    </select>
                </label>
            </div>
            <NarrativeCard domain={domain} />
            <AlertsStrip domain={domain} />
            <PlaybookCards />
            <RecommendationsCard domain={domain} />
            <TrainingGapCard windowDays={windowDays} />
        </div>
    );
}

// ─────────────────────────────────────────────────────────────────────
// Narrative
// ─────────────────────────────────────────────────────────────────────

function NarrativeCard({ domain }: { domain: Domain }) {
    const { data, isLoading } = useManagerNarrative();
    const refresh = useRefreshNarrative();

    if (isLoading) {
        return (
            <Card>
                <p className="text-text-muted">Loading…</p>
            </Card>
        );
    }
    if (!data) return null;

    const factors = Array.isArray(data.top_factors) ? data.top_factors : [];

    return (
        <Card>
            <div className="flex items-start justify-between gap-4">
                <div className="space-y-2">
                    <div className="text-xs uppercase tracking-wide text-text-subtle">
                        State of the {DOMAIN_LABEL[domain].toLowerCase()} motion
                    </div>
                    <p className="text-lg text-text">
                        {data.summary ||
                            "Not enough recent activity to summarize. The headline updates daily once interactions land."}
                    </p>
                    <div className="flex flex-wrap items-center gap-3 text-xs text-text-subtle">
                        {data.as_of && (
                            <span title="Time the headline was generated">
                                Updated {new Date(data.as_of).toLocaleString()}
                            </span>
                        )}
                        {data.confidence !== null && (
                            <span
                                title={
                                    "How sure the model is, based on how much recent " +
                                    "activity it had to read."
                                }
                            >
                                Confidence:{" "}
                                <strong className="text-text">
                                    {confidenceLabel(data.confidence)}
                                </strong>
                            </span>
                        )}
                    </div>
                </div>
                <button
                    onClick={() => refresh.mutate()}
                    disabled={refresh.isPending}
                    className="rounded border border-border bg-bg-card px-3 py-1 text-sm hover:bg-bg-card-hover disabled:opacity-50"
                    title="Generate a fresh headline now. Limited to once per hour."
                >
                    {refresh.isPending ? "Refreshing…" : "Refresh now"}
                </button>
            </div>
            {factors.length > 0 && (
                <div className="mt-3 flex flex-wrap gap-2">
                    {factors.map((factor, i) => (
                        <FactorChip key={i} factor={factor} />
                    ))}
                </div>
            )}
        </Card>
    );
}

function FactorChip({ factor }: { factor: unknown }) {
    if (typeof factor !== "object" || factor === null) {
        return <Chip>{String(factor)}</Chip>;
    }
    const f = factor as { label?: string; direction?: string };
    const dir = f.direction === "negative" ? "↓" : f.direction === "positive" ? "↑" : "•";
    return (
        <Chip>
            <span className="mr-1 font-semibold">{dir}</span>
            {f.label || "factor"}
        </Chip>
    );
}

function confidenceLabel(c: number): string {
    if (c >= 0.8) return "High";
    if (c >= 0.5) return "Medium";
    return "Low";
}

// ─────────────────────────────────────────────────────────────────────
// Alerts
// ─────────────────────────────────────────────────────────────────────

function AlertsStrip({ domain }: { domain: Domain }) {
    const { data: alerts = [], isLoading } = useManagerAlerts({
        onlyOpen: true,
        domain,
    });
    const ack = useAcknowledgeAlert();
    const dismiss = useDismissAlert();

    if (isLoading) return null;

    return (
        <section>
            <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-text-muted">
                Live alerts ({alerts.length})
            </h2>
            {alerts.length === 0 ? (
                <Card>
                    <p className="text-sm text-text-muted">
                        No open alerts for {DOMAIN_LABEL[domain].toLowerCase()}.
                        Anything new will show up here within a few minutes.
                    </p>
                </Card>
            ) : (
                <div className="grid gap-2 md:grid-cols-2">
                    {alerts.map((a) => (
                        <AlertCard
                            key={a.id}
                            alert={a}
                            onAck={() => ack.mutate(a.id)}
                            onDismiss={() => dismiss.mutate({ id: a.id })}
                        />
                    ))}
                </div>
            )}
        </section>
    );
}

function AlertCard({
    alert,
    onAck,
    onDismiss,
}: {
    alert: ManagerAlert;
    onAck: () => void;
    onDismiss: () => void;
}) {
    const evidence = alert.evidence || {};
    return (
        <div className="rounded-lg border border-border bg-bg-card p-3">
            <div className="flex items-center gap-2">
                <span
                    className={`rounded border px-2 py-0.5 text-xs font-semibold uppercase ${SEVERITY_BADGE[alert.severity]}`}
                >
                    {alert.severity}
                </span>
                <span className="text-xs text-text-subtle">
                    {ALERT_KIND_LABEL[alert.kind as AlertKind] ||
                        alert.kind.replace(/_/g, " ")}
                </span>
                <span className="ml-auto text-xs text-text-subtle">
                    {timeAgo(alert.opened_at)}
                </span>
            </div>
            <p className="mt-2 text-sm font-medium text-text">{alert.title}</p>
            {alert.body && (
                <p className="mt-1 text-xs text-text-muted">{alert.body}</p>
            )}
            <EvidenceChips evidence={evidence} />
            <div className="mt-3 flex gap-2">
                <button
                    onClick={onAck}
                    className="rounded border border-border bg-bg-card px-2 py-1 text-xs hover:bg-bg-card-hover"
                >
                    Acknowledge
                </button>
                <button
                    onClick={onDismiss}
                    className="rounded px-2 py-1 text-xs text-text-muted hover:bg-bg-card-hover"
                >
                    Dismiss
                </button>
            </div>
        </div>
    );
}

function EvidenceChips({ evidence }: { evidence: Record<string, unknown> }) {
    const chips: { label: string; value: string }[] = [];
    const push = (label: string, raw: unknown, suffix: string = "") => {
        if (raw === null || raw === undefined) return;
        if (typeof raw === "number") {
            chips.push({ label, value: `${formatNumber(raw)}${suffix}` });
        } else if (typeof raw === "string" && raw) {
            chips.push({ label, value: raw });
        }
    };
    push("Calls", evidence.current_count);
    push("Customers", evidence.customer_count);
    push("Avg now", evidence.current_avg);
    push("vs baseline", evidence.baseline_avg);
    push("Delta", evidence.delta);
    push("Pct change", evidence.pct_change, "%");
    push("Avg TTR", evidence.current_avg_hours, "h");

    if (chips.length === 0) return null;
    return (
        <div className="mt-2 flex flex-wrap gap-1">
            {chips.slice(0, 4).map((c, i) => (
                <Chip key={i}>
                    <span className="text-text-subtle">{c.label}:</span>{" "}
                    <strong className="ml-0.5 text-text">{c.value}</strong>
                </Chip>
            ))}
        </div>
    );
}

// ─────────────────────────────────────────────────────────────────────
// Playbook
// ─────────────────────────────────────────────────────────────────────

function PlaybookCards() {
    const { data: narrative } = useManagerNarrative();
    const playbook = (narrative?.playbook_insights || {}) as {
        what_works?: string[];
        what_doesnt?: string[];
        top_performing_phrases?: string[];
        winning_objection_handlers?: string[];
        common_failure_modes?: string[];
    };

    type Labeled = { label: string; text: string };
    const works: Labeled[] = [
        ...(playbook.what_works || []).map((t) => ({ label: "Win", text: t })),
        ...(playbook.top_performing_phrases || []).map((t) => ({
            label: "Phrase",
            text: t,
        })),
        ...(playbook.winning_objection_handlers || []).map((t) => ({
            label: "Rebuttal",
            text: t,
        })),
    ];
    const breaks: Labeled[] = [
        ...(playbook.what_doesnt || []).map((t) => ({ label: "Loss", text: t })),
        ...(playbook.common_failure_modes || []).map((t) => ({
            label: "Failure mode",
            text: t,
        })),
    ];

    return (
        <div className="grid gap-4 md:grid-cols-2">
            <Card>
                <h3 className="text-sm font-semibold text-text">What's working</h3>
                {works.length === 0 ? (
                    <p className="mt-2 text-sm text-text-muted">
                        Not enough completed calls to surface reproducible wins yet.
                    </p>
                ) : (
                    <ul className="mt-2 space-y-2 text-sm">
                        {works.slice(0, 6).map((item, i) => (
                            <li
                                key={i}
                                className="rounded border border-border bg-bg p-2"
                            >
                                <span className="mr-2 rounded bg-bg-card px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-text-subtle">
                                    {item.label}
                                </span>
                                {item.text}
                            </li>
                        ))}
                    </ul>
                )}
                {works.length > 6 && (
                    <p className="mt-2 text-xs text-text-subtle">
                        +{works.length - 6} more.
                    </p>
                )}
            </Card>
            <Card>
                <h3 className="text-sm font-semibold text-text">What's breaking</h3>
                {breaks.length === 0 ? (
                    <p className="mt-2 text-sm text-text-muted">
                        No persistent failure patterns flagged.
                    </p>
                ) : (
                    <ul className="mt-2 space-y-2 text-sm">
                        {breaks.slice(0, 6).map((item, i) => (
                            <li
                                key={i}
                                className="rounded border border-border bg-bg p-2"
                            >
                                <span className="mr-2 rounded bg-bg-card px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-text-subtle">
                                    {item.label}
                                </span>
                                {item.text}
                            </li>
                        ))}
                    </ul>
                )}
                {breaks.length > 6 && (
                    <p className="mt-2 text-xs text-text-subtle">
                        +{breaks.length - 6} more.
                    </p>
                )}
            </Card>
        </div>
    );
}

// ─────────────────────────────────────────────────────────────────────
// Recommendations
// ─────────────────────────────────────────────────────────────────────

function RecommendationsCard({ domain }: { domain: Domain }) {
    const { data: recs = [], isLoading } = useManagerRecommendations("open", {
        domain,
    });
    const apply = useApplyRecommendation();
    const dismiss = useDismissRecommendation();

    return (
        <section>
            <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-text-muted">
                Recommended moves
            </h2>
            <Card>
                {isLoading ? (
                    <p className="text-text-muted">Loading…</p>
                ) : recs.length === 0 ? (
                    <p className="text-sm text-text-muted">
                        No open recommendations for{" "}
                        {DOMAIN_LABEL[domain].toLowerCase()}. New suggestions
                        appear once a day after the overnight refresh.
                    </p>
                ) : (
                    <ul className="divide-y divide-border">
                        {recs.map((rec) => (
                            <RecommendationRow
                                key={rec.id}
                                rec={rec}
                                onApply={() => apply.mutate(rec.id)}
                                onDismiss={() => dismiss.mutate({ id: rec.id })}
                                applying={
                                    apply.isPending && apply.variables === rec.id
                                }
                                appliedArtifact={
                                    rec.status === "applied"
                                        ? {
                                              type: rec.applied_artifact_type,
                                              id: rec.applied_artifact_id,
                                          }
                                        : null
                                }
                            />
                        ))}
                    </ul>
                )}
            </Card>
        </section>
    );
}

const BRIEF_SECTION_LABEL: Record<string, string> = {
    situation: "Where things stand",
    why_now: "Why now",
    play: "The play",
    talking_points: "Talking points",
    draft_message: "Draft message",
    watch_out: "What to avoid",
    evidence: "The evidence",
    playbook: "Playbook",
    commitments: "Open commitments",
    success: "What success looks like",
};

function briefSectionLabel(kind: string): string {
    const known = BRIEF_SECTION_LABEL[kind];
    if (known) return known;
    const words = kind.replace(/_/g, " ");
    return words.charAt(0).toUpperCase() + words.slice(1);
}

function RecommendationRow({
    rec,
    onApply,
    onDismiss,
    applying,
    appliedArtifact,
}: {
    rec: ManagerRecommendation;
    onApply: () => void;
    onDismiss: () => void;
    applying: boolean;
    appliedArtifact: { type: string | null; id: string | null } | null;
}) {
    const [briefExpanded, setBriefExpanded] = useState(false);
    const evidence = rec.evidence as Record<string, unknown>;
    const callCount = (evidence?.call_count as number | undefined) ?? null;
    const customerCount = (evidence?.customer_count as number | undefined) ?? null;
    const dollarEstimate = (evidence?.dollar_estimate as number | undefined) ?? null;
    return (
        <li className="py-3">
            <div className="flex items-start justify-between gap-4">
                <div className="space-y-1">
                    <div className="text-xs uppercase tracking-wide text-text-subtle">
                        {CATEGORY_LABEL[rec.category] || rec.category}
                    </div>
                    <p className="text-sm font-medium text-text">{rec.title}</p>
                    {rec.brief ? (
                        <>
                            <p className="text-xs text-text-muted">
                                {rec.brief.headline}
                            </p>
                            <button
                                type="button"
                                onClick={() => setBriefExpanded((v) => !v)}
                                className="text-xs text-primary hover:underline"
                            >
                                {briefExpanded ? "Hide brief" : "Full brief"}
                            </button>
                            {briefExpanded && (
                                <div className="mt-2 space-y-3 rounded border border-border bg-bg-card-hover p-3">
                                    {rec.brief.sections.map((section, i) => (
                                        <div key={i}>
                                            <p className="text-xs font-bold text-text">
                                                {section.title ||
                                                    briefSectionLabel(section.kind)}
                                            </p>
                                            {section.body && (
                                                <p className="mt-1 whitespace-pre-line text-xs text-text-muted">
                                                    {section.body}
                                                </p>
                                            )}
                                            {section.items &&
                                                section.items.length > 0 && (
                                                    <ul className="mt-1 list-disc space-y-0.5 pl-4 text-xs text-text-muted">
                                                        {section.items.map(
                                                            (item, j) => (
                                                                <li key={j}>{item}</li>
                                                            )
                                                        )}
                                                    </ul>
                                                )}
                                        </div>
                                    ))}
                                </div>
                            )}
                        </>
                    ) : (
                        rec.rationale && (
                            <p className="text-xs text-text-muted">{rec.rationale}</p>
                        )
                    )}
                    <div className="mt-1 flex flex-wrap gap-2 text-xs text-text-subtle">
                        {callCount !== null && <Chip>{callCount} calls</Chip>}
                        {customerCount !== null && (
                            <Chip>{customerCount} customers</Chip>
                        )}
                        {dollarEstimate !== null && (
                            <Chip>
                                Estimated impact:{" "}
                                <strong className="ml-1 text-text">
                                    {formatDollars(dollarEstimate)}
                                </strong>
                            </Chip>
                        )}
                        <Chip>Priority: {impactBand(rec.score)}</Chip>
                    </div>
                    {appliedArtifact && (
                        <p className="mt-2 text-xs text-text-muted">
                            Applied. Created{" "}
                            {appliedArtifact.type
                                ? appliedArtifact.type.replace(/_/g, " ")
                                : "artifact"}
                            {appliedArtifact.id
                                ? ` ${appliedArtifact.id.slice(0, 8)}…`
                                : ""}
                            .
                        </p>
                    )}
                </div>
                <div className="flex shrink-0 flex-col gap-2">
                    <button
                        onClick={onApply}
                        disabled={applying || !!appliedArtifact}
                        className="rounded bg-primary px-3 py-1 text-xs font-semibold text-bg disabled:opacity-50"
                    >
                        {applying
                            ? "Applying…"
                            : appliedArtifact
                            ? "Applied"
                            : "Apply"}
                    </button>
                    <button
                        onClick={onDismiss}
                        className="rounded px-3 py-1 text-xs text-text-muted hover:bg-bg-card-hover"
                    >
                        Dismiss
                    </button>
                </div>
            </div>
        </li>
    );
}

function impactBand(score: number): string {
    if (score >= 75) return "High";
    if (score >= 40) return "Medium";
    return "Low";
}

function formatDollars(n: number): string {
    if (!Number.isFinite(n)) return "$0";
    if (n >= 1_000_000) return `$${(n / 1_000_000).toFixed(1)}M`;
    if (n >= 1_000) return `$${(n / 1_000).toFixed(1)}k`;
    return `$${Math.round(n).toLocaleString()}`;
}

function formatNumber(n: number): string {
    if (!Number.isFinite(n)) return "—";
    if (Math.abs(n) >= 1000) {
        return n.toLocaleString(undefined, { maximumFractionDigits: 1 });
    }
    return n.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

function timeAgo(iso: string): string {
    const then = new Date(iso).getTime();
    if (!Number.isFinite(then)) return "";
    const diffMs = Date.now() - then;
    const minutes = Math.round(diffMs / 60_000);
    if (minutes < 1) return "just now";
    if (minutes < 60) return `${minutes}m ago`;
    const hours = Math.round(minutes / 60);
    if (hours < 24) return `${hours}h ago`;
    const days = Math.round(hours / 24);
    return `${days}d ago`;
}

// ─────────────────────────────────────────────────────────────────────
// Drill-down: training gap (with column tooltips)
// ─────────────────────────────────────────────────────────────────────

const TRAINING_GAP_HEADERS = {
    rep: { label: "Rep", title: "" },
    calls: {
        label: "Calls",
        title: "Number of analyzed interactions in the window.",
    },
    reflection: {
        label: "Reflection",
        title:
            "How often the rep summarized or paraphrased what the customer just said. Higher is better; >50% is typical for top performers.",
    },
    open_questions: {
        label: "Open questions",
        title:
            "Share of the rep's questions that were open-ended (couldn't be answered yes/no). Higher correlates with stronger discovery.",
    },
    methodology: {
        label: "Methodology",
        title:
            "Average share of methodology stages the rep covered per call (SPIN, MEDDIC, or structured-resolution depending on the tenant). >70% is the team's target.",
    },
} as const;

function TrainingGapCard({ windowDays }: { windowDays: number }) {
    const { data, isLoading } = useTrainingGap(windowDays);
    const rows = data?.rows || [];

    return (
        <section>
            <details className="rounded-lg border border-border bg-bg-card">
                <summary className="cursor-pointer select-none px-4 py-3 text-sm font-semibold text-text">
                    Training-gap drill-down (per rep)
                </summary>
                <div className="border-t border-border p-4">
                    {isLoading ? (
                        <p className="text-text-muted text-sm">Loading…</p>
                    ) : rows.length === 0 ? (
                        <p className="text-text-muted text-sm">
                            No reps with calls in the selected window.
                        </p>
                    ) : (
                        <table className="w-full text-sm">
                            <thead className="text-xs uppercase text-text-subtle">
                                <tr>
                                    {(
                                        [
                                            "rep",
                                            "calls",
                                            "reflection",
                                            "open_questions",
                                            "methodology",
                                        ] as const
                                    ).map((key) => (
                                        <th
                                            key={key}
                                            className={
                                                key === "rep"
                                                    ? "text-left"
                                                    : "text-right"
                                            }
                                            title={TRAINING_GAP_HEADERS[key].title}
                                        >
                                            {TRAINING_GAP_HEADERS[key].label}
                                        </th>
                                    ))}
                                </tr>
                            </thead>
                            <tbody>
                                {rows.map((r) => (
                                    <tr
                                        key={r.rep_id ?? "unknown"}
                                        className="border-t border-border"
                                    >
                                        <td>{r.rep_name || "—"}</td>
                                        <td className="text-right">{r.call_count}</td>
                                        <td className="text-right">{pct(r.reflection_rate)}</td>
                                        <td className="text-right">{pct(r.open_question_rate)}</td>
                                        <td className="text-right">{pct(r.avg_methodology_coverage)}</td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    )}
                </div>
            </details>
        </section>
    );
}

function pct(v: number | null) {
    if (v === null || v === undefined) return "—";
    return `${(v * 100).toFixed(0)}%`;
}

// ─────────────────────────────────────────────────────────────────────
// Journey view (cross-motion)
// ─────────────────────────────────────────────────────────────────────

function JourneyView() {
    const [windowDays, setWindowDays] = useState(90);
    const { data, isLoading } = useJourney(windowDays);

    return (
        <div className="space-y-6">
            <div className="flex items-baseline justify-between gap-2">
                <p className="text-sm text-text-muted">
                    How customers are moving across motions. Look here for
                    handoffs that stalled.
                </p>
                <label className="text-sm text-text-muted">
                    Window:{" "}
                    <select
                        value={windowDays}
                        onChange={(e) =>
                            setWindowDays(parseInt(e.target.value, 10))
                        }
                        className="ml-1 rounded border border-border bg-bg-card px-2 py-1 text-sm text-text"
                    >
                        <option value={30}>Last 30 days</option>
                        <option value={90}>Last 90 days</option>
                        <option value={180}>Last 180 days</option>
                    </select>
                </label>
            </div>

            {isLoading ? (
                <Card>
                    <p className="text-text-muted">Loading…</p>
                </Card>
            ) : !data ? (
                <Card>
                    <p className="text-text-muted">No journey data available yet.</p>
                </Card>
            ) : (
                <>
                    <HandoffStrip rows={data.handoffs} />
                    <AccountsAtRiskTable rows={data.accounts_at_risk} />
                </>
            )}
        </div>
    );
}

function HandoffStrip({ rows }: { rows: JourneyHandoffRow[] }) {
    if (rows.length === 0) {
        return (
            <Card>
                <h3 className="text-sm font-semibold text-text">Handoff health</h3>
                <p className="mt-2 text-sm text-text-muted">
                    No stalled handoffs detected in the selected window. Customers
                    are moving between Sales, Customer Success, and Support as
                    expected.
                </p>
            </Card>
        );
    }
    return (
        <section>
            <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-text-muted">
                Handoff health
            </h2>
            <div className="grid gap-2 md:grid-cols-3">
                {rows.map((r) => (
                    <Card key={r.transition}>
                        <p className="text-xs uppercase tracking-wide text-text-subtle">
                            {transitionLabel(r.transition)}
                        </p>
                        <p className="mt-2 text-2xl font-semibold text-text">
                            {r.customer_count}
                        </p>
                        <p className="text-xs text-text-muted">
                            customers stuck
                            {r.avg_days_stalled !== null
                                ? `, avg ${r.avg_days_stalled.toFixed(0)} days`
                                : ""}
                        </p>
                    </Card>
                ))}
            </div>
        </section>
    );
}

function transitionLabel(t: JourneyHandoffRow["transition"]): string {
    if (t === "sales_to_cs") return "Sales → Customer Success";
    if (t === "cs_to_support") return "Customer Success → Support";
    if (t === "support_to_renewal") return "Support → Renewal";
    return t;
}

function AccountsAtRiskTable({ rows }: { rows: JourneyAccountRow[] }) {
    if (rows.length === 0) {
        return (
            <Card>
                <h3 className="text-sm font-semibold text-text">Accounts at risk</h3>
                <p className="mt-2 text-sm text-text-muted">
                    No accounts are showing cross-motion risk signals right now.
                </p>
            </Card>
        );
    }
    return (
        <section>
            <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-text-muted">
                Accounts at risk across motions
            </h2>
            <Card>
                <table className="w-full text-sm">
                    <thead className="text-xs uppercase text-text-subtle">
                        <tr>
                            <th className="text-left">Account</th>
                            <th className="text-right" title="Last Sales activity">
                                Sales
                            </th>
                            <th className="text-right" title="Last CS activity">
                                CS
                            </th>
                            <th
                                className="text-right"
                                title="Last Support activity"
                            >
                                Support
                            </th>
                            <th
                                className="text-right"
                                title="Currently open support cases"
                            >
                                Open cases
                            </th>
                            <th
                                className="text-right"
                                title="Latest churn-risk signal observed across all motions"
                            >
                                Risk
                            </th>
                        </tr>
                    </thead>
                    <tbody>
                        {rows.map((r) => (
                            <tr
                                key={r.customer_id}
                                className="border-t border-border"
                            >
                                <td className="py-1">
                                    {r.customer_name || r.customer_id.slice(0, 8)}
                                </td>
                                <td className="text-right text-text-subtle">
                                    {dateOnly(r.last_sales_at)}
                                </td>
                                <td className="text-right text-text-subtle">
                                    {dateOnly(r.last_cs_at)}
                                </td>
                                <td className="text-right text-text-subtle">
                                    {dateOnly(r.last_support_at)}
                                </td>
                                <td className="text-right">
                                    {r.open_support_cases > 0 ? (
                                        <strong className="text-text">
                                            {r.open_support_cases}
                                        </strong>
                                    ) : (
                                        <span className="text-text-subtle">0</span>
                                    )}
                                </td>
                                <td className="text-right">
                                    {r.health_signal ? (
                                        <RiskPill signal={r.health_signal} />
                                    ) : (
                                        <span className="text-text-subtle">—</span>
                                    )}
                                </td>
                            </tr>
                        ))}
                    </tbody>
                </table>
            </Card>
        </section>
    );
}

function RiskPill({ signal }: { signal: string }) {
    const cls =
        signal === "high"
            ? "bg-error-soft text-error border-error"
            : signal === "medium"
            ? "bg-amber-100 text-amber-700 border-amber-300"
            : "bg-blue-50 text-blue-700 border-blue-200";
    return (
        <span
            className={`rounded border px-2 py-0.5 text-[10px] font-semibold uppercase ${cls}`}
        >
            {signal}
        </span>
    );
}

function dateOnly(iso: string | null): string {
    if (!iso) return "—";
    try {
        return new Date(iso).toLocaleDateString();
    } catch {
        return "—";
    }
}

// ─────────────────────────────────────────────────────────────────────
// Layout primitives
// ─────────────────────────────────────────────────────────────────────

function Card({ children }: { children: React.ReactNode }) {
    return (
        <div className="rounded-lg border border-border bg-bg-card p-4">
            {children}
        </div>
    );
}

function Chip({ children }: { children: React.ReactNode }) {
    return (
        <span className="inline-flex items-center rounded border border-border bg-bg px-2 py-0.5 text-xs">
            {children}
        </span>
    );
}
