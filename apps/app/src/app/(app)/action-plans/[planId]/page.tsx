"use client";

import { useParams } from "next/navigation";
import Link from "next/link";

import { useActionPlan } from "@/lib/action-plans";
import { PlanCanvas } from "@/components/action-plan/canvas";
import { PlanHeader } from "@/components/action-plan/plan-header";

export default function ActionPlanDetailPage() {
    const params = useParams<{ planId: string }>();
    const planId = params?.planId;
    const { data: plan, isLoading, error } = useActionPlan(planId);

    return (
        <div className="mx-auto max-w-4xl space-y-4 p-4">
            <Link
                href="/action-plans"
                className="text-xs text-indigo-600 hover:underline dark:text-indigo-400"
            >
                ← All plans
            </Link>
            {isLoading && (
                <p className="text-sm text-slate-500">Loading plan…</p>
            )}
            {error && (
                <p className="text-sm text-rose-600">Plan failed to load.</p>
            )}
            {plan && (
                <>
                    <PlanHeader plan={plan} />
                    <PlanCanvas plan={plan} />
                </>
            )}
        </div>
    );
}
