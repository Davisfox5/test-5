"use client";

/**
 * Vertical swim-lane canvas for an Action Plan.
 *
 * Per the locked UI decision: a top-to-bottom column of step cards
 * with SVG arrows in a left gutter showing dependencies. The
 * customer-facing endpoint card is pinned at the bottom with a double
 * border so the rep always knows what the plan is building toward.
 *
 * Edge labels show the slot_key each arrow carries — concrete data
 * flow, not abstract dependency.
 */
import { useMemo } from "react";

import { ActionPlan, ActionStep } from "@/lib/action-plans";
import { StepCard } from "./step-card";

export interface PlanCanvasProps {
    plan: ActionPlan;
}

/**
 * Topologically sort steps so any step's ``depends_on`` predecessors
 * appear before it. Stable: ties (no dependency edge between two
 * steps) break by ``created_at`` so the synthesizer's emission order
 * shows through where it doesn't conflict with the DAG.
 *
 * Kahn's algorithm. Cross-group dependencies (e.g. customer_endpoint
 * depends on a preparation step) are honored at the macro level by
 * the role grouping itself; this sort is mainly about WITHIN-group
 * ordering ("Brief Rajiv depends on Log CRM, so Log CRM should be
 * listed first inside the preparation lane").
 *
 * Cycles can't exist in practice (Call B builds a DAG), but defend
 * anyway: if a cycle is detected, fall back to created_at order so
 * the rep at least sees every step rather than nothing.
 */
function topologicalSort(steps: ActionStep[]): ActionStep[] {
    const byId = new Map(steps.map((s) => [s.id, s]));
    const inScope = new Set(steps.map((s) => s.id));
    const remainingDeps = new Map<string, Set<string>>();
    const dependents = new Map<string, string[]>();

    for (const s of steps) {
        const deps = (s.depends_on || []).filter((d) => inScope.has(d));
        remainingDeps.set(s.id, new Set(deps));
        for (const d of deps) {
            const list = dependents.get(d) ?? [];
            list.push(s.id);
            dependents.set(d, list);
        }
    }

    // Initial frontier: steps with no remaining deps inside this scope,
    // ordered by created_at so emission order is the tie-break.
    const ready: ActionStep[] = steps
        .filter((s) => (remainingDeps.get(s.id) || new Set()).size === 0)
        .sort(
            (a, b) =>
                new Date(a.created_at).getTime() - new Date(b.created_at).getTime(),
        );

    const out: ActionStep[] = [];
    const seen = new Set<string>();
    while (ready.length > 0) {
        const next = ready.shift()!;
        if (seen.has(next.id)) continue;
        seen.add(next.id);
        out.push(next);
        for (const dependentId of dependents.get(next.id) || []) {
            const remaining = remainingDeps.get(dependentId);
            if (!remaining) continue;
            remaining.delete(next.id);
            if (remaining.size === 0) {
                const dep = byId.get(dependentId);
                if (dep && !seen.has(dep.id)) {
                    // Insert ordered by created_at so within a single
                    // "frontier batch" the synthesizer's emission
                    // order still shows through.
                    const insertAt = ready.findIndex(
                        (r) =>
                            new Date(r.created_at).getTime() >
                            new Date(dep.created_at).getTime(),
                    );
                    if (insertAt === -1) ready.push(dep);
                    else ready.splice(insertAt, 0, dep);
                }
            }
        }
    }

    // Defensive: if a cycle (or any orphan) prevented us from emitting
    // every step, append the missing ones in created_at order so they
    // still render. This shouldn't happen on a synthesized plan.
    if (out.length !== steps.length) {
        const leftovers = steps
            .filter((s) => !seen.has(s.id))
            .sort(
                (a, b) =>
                    new Date(a.created_at).getTime() -
                    new Date(b.created_at).getTime(),
            );
        out.push(...leftovers);
    }
    return out;
}

function visibleSteps(plan: ActionPlan): ActionStep[] {
    const ROLE_ORDER = {
        preparation: 0,
        customer_endpoint: 1,
        post_completion: 2,
    } as const;
    const visible = plan.steps.filter((s) => s.state !== "deleted");

    // Group by role first, then topo-sort each group. A step depending
    // on another step in the SAME group needs to appear after it; a
    // step depending on a step in an EARLIER role group is fine because
    // role group ordering already enforces the macro chronology.
    const byRole = new Map<string, ActionStep[]>();
    for (const s of visible) {
        const key = s.role_in_plan || "preparation";
        const list = byRole.get(key) ?? [];
        list.push(s);
        byRole.set(key, list);
    }

    const roles = Array.from(byRole.keys()).sort(
        (a, b) =>
            (ROLE_ORDER[a as keyof typeof ROLE_ORDER] ?? 99) -
            (ROLE_ORDER[b as keyof typeof ROLE_ORDER] ?? 99),
    );

    const out: ActionStep[] = [];
    for (const role of roles) {
        out.push(...topologicalSort(byRole.get(role) ?? []));
    }
    return out;
}

function slotKeyByEdge(plan: ActionPlan): Map<string, string[]> {
    // Map: depends_on step_id -> [slot_key, ...] flowing into the consumer.
    // Used to render an arrow chip with the slot label.
    const out = new Map<string, string[]>();
    for (const step of plan.steps) {
        for (const slot of step.input_slots) {
            if (!slot.filled_by_step_id) continue;
            const key = `${slot.filled_by_step_id}->${step.id}`;
            const existing = out.get(key) ?? [];
            existing.push(slot.slot_key);
            out.set(key, existing);
        }
    }
    return out;
}

export function PlanCanvas({ plan }: PlanCanvasProps) {
    const ordered = useMemo(() => visibleSteps(plan), [plan]);
    const edgeLabels = useMemo(() => slotKeyByEdge(plan), [plan]);

    return (
        <div className="space-y-3">
            {ordered.map((step, idx) => {
                const upstreamIds = step.depends_on || [];
                const upstreamArrows = upstreamIds
                    .map((upId) => {
                        const upstreamStep = ordered.find((s) => s.id === upId);
                        if (!upstreamStep) return null;
                        const slots = edgeLabels.get(`${upId}->${step.id}`) ?? [];
                        return {
                            upstreamTitle: upstreamStep.title,
                            slotKeys: slots,
                        };
                    })
                    .filter(Boolean) as Array<{ upstreamTitle: string; slotKeys: string[] }>;

                const isFirst = idx === 0;
                const isEndpoint =
                    plan.customer_endpoint_step_id === step.id
                    || step.role_in_plan === "customer_endpoint";

                return (
                    <div key={step.id} className="space-y-2">
                        {!isFirst && upstreamArrows.length > 0 && (
                            <div className="ml-4 border-l-2 border-slate-300 pl-3 text-xs text-slate-500 dark:border-slate-700 dark:text-slate-400">
                                {upstreamArrows.map((arr, i) => (
                                    <div key={i} className="py-1">
                                        <span className="mr-1">↓</span>
                                        <span className="font-medium">{arr.upstreamTitle}</span>
                                        {arr.slotKeys.length > 0 && (
                                            <span className="ml-2 rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[10px] dark:bg-slate-800">
                                                {arr.slotKeys.join(", ")}
                                            </span>
                                        )}
                                    </div>
                                ))}
                            </div>
                        )}
                        <StepCard
                            plan={plan}
                            step={step}
                            highlightAsEndpoint={isEndpoint}
                        />
                    </div>
                );
            })}
            {ordered.length === 0 && (
                <div className="rounded border border-dashed border-slate-300 p-6 text-center text-sm text-slate-500 dark:border-slate-700 dark:text-slate-400">
                    No follow-up required for this call.
                </div>
            )}
        </div>
    );
}
