"use client";

import { ActionPlan } from "@/lib/action-plans";

interface PlanHeaderProps {
    plan: ActionPlan;
}

function progressCounts(plan: ActionPlan): {
    done: number;
    ready: number;
    blocked: number;
    awaiting: number;
    total: number;
} {
    const live = plan.steps.filter((s) => s.state !== "deleted");
    const done = live.filter((s) => s.state === "done" || s.state === "skipped").length;
    const ready = live.filter(
        (s) => s.state === "ready" || s.state === "in_progress",
    ).length;
    const awaiting = live.filter((s) => s.state === "awaiting_response").length;
    const blocked = live.filter((s) => s.state === "blocked").length;
    return { done, ready, blocked, awaiting, total: live.length };
}

export function PlanHeader({ plan }: PlanHeaderProps) {
    const { done, ready, blocked, awaiting, total } = progressCounts(plan);
    const externalSnap = plan.external_context_snapshot as { snapshots?: Array<{ provider: string; is_stale?: boolean }> } | null;
    const staleProviders = (externalSnap?.snapshots ?? [])
        .filter((s) => s.is_stale)
        .map((s) => s.provider);

    const summary =
        plan.status === "completed"
            ? "Plan complete."
            : ready > 0
              ? `${ready} step${ready === 1 ? "" : "s"} ready to do now${blocked ? `, ${blocked} blocked behind earlier steps` : ""}.`
              : awaiting > 0
                ? `Waiting on ${awaiting} response${awaiting === 1 ? "" : "s"}.`
                : blocked > 0
                  ? `${blocked} step${blocked === 1 ? "" : "s"} blocked.`
                  : "No active steps.";

    return (
        <div className="space-y-2 border-b border-slate-200 pb-3 dark:border-slate-700">
            <h2 className="text-base font-semibold">
                {plan.goal ?? "Untitled plan"}
            </h2>
            <div className="flex flex-wrap items-center gap-3 text-xs">
                <span className="text-text-muted">
                    <span className="font-medium text-text">{done}</span> of{" "}
                    <span className="font-medium text-text">{total}</span> done
                </span>
                {ready > 0 && (
                    <span className="rounded bg-emerald-100 px-1.5 py-0.5 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-200">
                        {ready} ready
                    </span>
                )}
                {awaiting > 0 && (
                    <span className="rounded bg-amber-100 px-1.5 py-0.5 text-amber-800 dark:bg-amber-900/40 dark:text-amber-200">
                        {awaiting} awaiting response
                    </span>
                )}
                {blocked > 0 && (
                    <span className="rounded bg-slate-200 px-1.5 py-0.5 text-slate-600 dark:bg-slate-800 dark:text-slate-300">
                        {blocked} blocked
                    </span>
                )}
            </div>
            <p className="text-xs text-text-muted">{summary}</p>
            {staleProviders.length > 0 ? (
                <p className="text-xs text-amber-700 dark:text-amber-300">
                    CRM data stale for: {staleProviders.join(", ")}. Live
                    refresh failed; using last-known values.
                </p>
            ) : null}
        </div>
    );
}
