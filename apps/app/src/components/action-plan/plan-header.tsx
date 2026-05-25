"use client";

import { ActionPlan } from "@/lib/action-plans";

interface PlanHeaderProps {
    plan: ActionPlan;
}

const DOMAIN_LABEL: Record<string, string> = {
    sales: "Sales",
    customer_service: "Customer Service",
    it_support: "IT Support",
    generic: "Generic",
};

const STATUS_LABEL: Record<string, string> = {
    draft: "Draft",
    active: "Active",
    completed: "Completed",
    abandoned: "Abandoned",
};

function progressCounts(plan: ActionPlan): { done: number; total: number } {
    const live = plan.steps.filter((s) => s.state !== "deleted");
    const done = live.filter((s) => s.state === "done" || s.state === "skipped").length;
    return { done, total: live.length };
}

export function PlanHeader({ plan }: PlanHeaderProps) {
    const { done, total } = progressCounts(plan);
    const externalSnap = plan.external_context_snapshot as { snapshots?: Array<{ provider: string; is_stale?: boolean }> } | null;
    const staleProviders = (externalSnap?.snapshots ?? [])
        .filter((s) => s.is_stale)
        .map((s) => s.provider);

    return (
        <div className="space-y-2 border-b border-slate-200 pb-3 dark:border-slate-700">
            <div className="flex flex-wrap items-center gap-2">
                <span className="rounded bg-indigo-100 px-2 py-0.5 text-xs font-medium text-indigo-900 dark:bg-indigo-900/40 dark:text-indigo-100">
                    {DOMAIN_LABEL[plan.domain] ?? plan.domain}
                </span>
                <span className="rounded bg-slate-200 px-2 py-0.5 text-xs text-slate-700 dark:bg-slate-800 dark:text-slate-300">
                    {STATUS_LABEL[plan.status] ?? plan.status}
                </span>
                <span className="text-xs text-slate-500">
                    Progress: {done}/{total}
                </span>
                {plan.procedures_applied.length > 0 ? (
                    <span className="text-xs text-slate-500">
                        Procedures: {plan.procedures_applied.length}
                    </span>
                ) : null}
            </div>
            <h2 className="text-base font-semibold">
                {plan.goal ?? "Untitled plan"}
            </h2>
            {staleProviders.length > 0 ? (
                <p className="text-xs text-amber-700 dark:text-amber-300">
                    CRM data stale for: {staleProviders.join(", ")}. Live refresh failed; using last-known values.
                </p>
            ) : null}
        </div>
    );
}
