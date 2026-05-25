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

function visibleSteps(plan: ActionPlan): ActionStep[] {
    return plan.steps
        .filter((s) => s.state !== "deleted")
        .sort((a, b) => {
            // Custom ordering: preparation -> customer_endpoint -> post_completion,
            // then by created_at within each group so the swim lane reads
            // logically.
            const order = {
                preparation: 0,
                customer_endpoint: 1,
                post_completion: 2,
            } as const;
            const ra = order[a.role_in_plan as keyof typeof order] ?? 0;
            const rb = order[b.role_in_plan as keyof typeof order] ?? 0;
            if (ra !== rb) return ra - rb;
            return new Date(a.created_at).getTime() - new Date(b.created_at).getTime();
        });
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
