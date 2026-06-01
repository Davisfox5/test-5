"use client";

/**
 * Customer relationship memory view.
 *
 * Renders LINDA's living per-customer profile: every tracked concern
 * with its lifecycle status and every customer-side commitment with
 * its current state. Lives next to the existing customer detail page
 * (which covers contacts + interactions).
 */

import Link from "next/link";
import { useParams } from "next/navigation";
import { useMe } from "@/lib/me";
import { DOMAIN_LABEL } from "@/lib/manager";
import {
    Commitment,
    COMMITMENT_STATUS_LABEL,
    Concern,
    STATUS_COLORS,
    STATUS_LABEL,
    useCustomerMemory,
    usePatchCommitment,
    usePatchConcern,
} from "@/lib/customer_memory";

export default function CustomerMemoryPage() {
    const params = useParams();
    const id =
        typeof params.id === "string" ? params.id : params.id?.[0] || null;
    const { data, isLoading } = useCustomerMemory(id);
    const me = useMe();
    const canEdit =
        me.data?.user?.is_tenant_admin === true ||
        (me.data?.user?.manager_domains?.length ?? 0) > 0;

    const patchConcern = usePatchConcern(id || "");
    const patchCommitment = usePatchCommitment(id || "");

    if (!id || isLoading) return <p className="text-text-muted">Loading…</p>;
    if (!data) {
        return (
            <div className="rounded-lg border border-border bg-bg-card p-4">
                <p className="text-text">No memory yet for this account.</p>
                <Link
                    href="/customers"
                    className="mt-2 inline-block text-sm underline"
                >
                    Back to accounts
                </Link>
            </div>
        );
    }

    return (
        <div className="space-y-6">
            <header className="space-y-1">
                <Link
                    href={`/customers/${id}`}
                    className="text-xs text-text-muted hover:underline"
                >
                    ← Back to {data.customer_name}
                </Link>
                <h1 className="text-2xl font-bold">
                    Relationship memory — {data.customer_name}
                </h1>
                <p className="text-sm text-text-muted">
                    Every concern LINDA has tracked, every commitment they
                    made. Updated automatically from every call and email.
                </p>
            </header>

            <section>
                <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-text-muted">
                    Concerns ({data.concerns.length})
                </h2>
                {data.concerns.length === 0 ? (
                    <p className="rounded-lg border border-border bg-bg-card p-4 text-sm text-text-muted">
                        No concerns surfaced yet. They'll appear here when an
                        interaction flags one.
                    </p>
                ) : (
                    <ul className="space-y-2">
                        {data.concerns.map((c) => (
                            <ConcernCard
                                key={c.id}
                                concern={c}
                                canEdit={canEdit}
                                onChangeStatus={(s) =>
                                    patchConcern.mutate({
                                        id: c.id,
                                        patch: { status: s },
                                    })
                                }
                            />
                        ))}
                    </ul>
                )}
            </section>

            <section>
                <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-text-muted">
                    Their commitments ({data.commitments.length})
                </h2>
                {data.commitments.length === 0 ? (
                    <p className="rounded-lg border border-border bg-bg-card p-4 text-sm text-text-muted">
                        No customer-side commitments tracked yet. (Commitments
                        we make to them live in the Action Items inbox.)
                    </p>
                ) : (
                    <ul className="space-y-2">
                        {data.commitments.map((cm) => (
                            <CommitmentCard
                                key={cm.id}
                                commitment={cm}
                                canEdit={canEdit}
                                onChangeStatus={(s) =>
                                    patchCommitment.mutate({
                                        id: cm.id,
                                        patch: { status: s },
                                    })
                                }
                            />
                        ))}
                    </ul>
                )}
            </section>
        </div>
    );
}

function ConcernCard({
    concern,
    canEdit,
    onChangeStatus,
}: {
    concern: Concern;
    canEdit: boolean;
    onChangeStatus: (s: Concern["status"]) => void;
}) {
    return (
        <li className="rounded-lg border border-border bg-bg-card p-3">
            <div className="flex items-start justify-between gap-3">
                <div className="space-y-1">
                    <div className="flex items-center gap-2">
                        <p className="text-sm font-medium text-text">
                            {concern.topic.replace(/_/g, " ")}
                        </p>
                        <span
                            className={`rounded border px-2 py-0.5 text-[10px] font-semibold uppercase ${STATUS_COLORS[concern.status]}`}
                        >
                            {STATUS_LABEL[concern.status]}
                        </span>
                        <span className="text-[10px] uppercase tracking-wide text-text-subtle">
                            {concern.severity}
                        </span>
                    </div>
                    {concern.description && (
                        <p className="text-xs text-text-muted">
                            {concern.description}
                        </p>
                    )}
                    <p className="text-xs text-text-subtle">
                        First seen{" "}
                        {new Date(concern.first_seen_at).toLocaleDateString()}{" "}
                        {concern.source_motion
                            ? `via ${DOMAIN_LABEL[concern.source_motion]}`
                            : ""}{" "}
                        · Last mentioned{" "}
                        {new Date(concern.last_seen_at).toLocaleDateString()}{" "}
                        · {concern.evidence_count} mention
                        {concern.evidence_count === 1 ? "" : "s"}
                    </p>
                </div>
                {canEdit && (
                    <select
                        value={concern.status}
                        onChange={(e) =>
                            onChangeStatus(e.target.value as Concern["status"])
                        }
                        className="rounded border border-border bg-bg-card px-2 py-1 text-xs"
                    >
                        {(
                            [
                                "active",
                                "monitoring",
                                "resolved",
                                "dormant",
                            ] as Concern["status"][]
                        ).map((s) => (
                            <option key={s} value={s}>
                                {STATUS_LABEL[s]}
                            </option>
                        ))}
                    </select>
                )}
            </div>
        </li>
    );
}

function CommitmentCard({
    commitment,
    canEdit,
    onChangeStatus,
}: {
    commitment: Commitment;
    canEdit: boolean;
    onChangeStatus: (s: Commitment["status"]) => void;
}) {
    return (
        <li className="rounded-lg border border-border bg-bg-card p-3">
            <div className="flex items-start justify-between gap-3">
                <div className="space-y-1">
                    <p className="text-sm font-medium text-text">
                        {commitment.description}
                    </p>
                    {commitment.quote && (
                        <p className="text-xs italic text-text-muted">
                            “{commitment.quote}”
                        </p>
                    )}
                    <p className="text-xs text-text-subtle">
                        {commitment.due_date
                            ? `Due ${new Date(commitment.due_date).toLocaleDateString()}`
                            : "No due date"}{" "}
                        · Added{" "}
                        {new Date(commitment.created_at).toLocaleDateString()}
                    </p>
                </div>
                {canEdit && (
                    <select
                        value={commitment.status}
                        onChange={(e) =>
                            onChangeStatus(
                                e.target.value as Commitment["status"],
                            )
                        }
                        className="rounded border border-border bg-bg-card px-2 py-1 text-xs"
                    >
                        {(
                            [
                                "open",
                                "met",
                                "broken",
                                "dismissed",
                            ] as Commitment["status"][]
                        ).map((s) => (
                            <option key={s} value={s}>
                                {COMMITMENT_STATUS_LABEL[s]}
                            </option>
                        ))}
                    </select>
                )}
            </div>
        </li>
    );
}
