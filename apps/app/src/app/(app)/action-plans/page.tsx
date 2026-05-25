"use client";

import Link from "next/link";
import { useState } from "react";

import { useActionPlans } from "@/lib/action-plans";

const TABS = [
    { key: "active", label: "Active" },
    { key: "completed", label: "Completed" },
    { key: "abandoned", label: "Abandoned" },
] as const;

type TabKey = (typeof TABS)[number]["key"];

export default function ActionPlansListPage() {
    const [tab, setTab] = useState<TabKey>("active");
    const { data, isLoading, error } = useActionPlans({ status: tab });

    return (
        <div className="mx-auto max-w-4xl space-y-4 p-4">
            <div>
                <h1 className="text-xl font-semibold">Action plans</h1>
                <p className="text-sm text-slate-600 dark:text-slate-300">
                    Workflows synthesized from each call, anchored in your KB
                    and connected systems.
                </p>
            </div>

            <div className="flex gap-2 border-b border-slate-200 dark:border-slate-700">
                {TABS.map((t) => (
                    <button
                        key={t.key}
                        type="button"
                        className={[
                            "px-3 py-2 text-sm",
                            tab === t.key
                                ? "border-b-2 border-indigo-600 font-medium text-indigo-700 dark:border-indigo-400 dark:text-indigo-200"
                                : "text-slate-500 hover:text-slate-700 dark:hover:text-slate-200",
                        ].join(" ")}
                        onClick={() => setTab(t.key)}
                    >
                        {t.label}
                    </button>
                ))}
            </div>

            {isLoading && (
                <p className="text-sm text-slate-500">Loading plans…</p>
            )}
            {error && (
                <p className="text-sm text-rose-600">
                    Failed to load plans. Try refreshing.
                </p>
            )}
            {data && data.items.length === 0 && (
                <p className="text-sm text-slate-500">
                    No {tab} plans.
                </p>
            )}
            <ul className="space-y-2">
                {data?.items.map((plan) => {
                    const live = plan.steps.filter((s) => s.state !== "deleted");
                    const done = live.filter((s) => s.state === "done" || s.state === "skipped").length;
                    return (
                        <li
                            key={plan.id}
                            className="rounded border border-slate-200 p-3 hover:border-indigo-300 dark:border-slate-700"
                        >
                            <Link href={`/action-plans/${plan.id}`} className="block">
                                <div className="flex flex-wrap items-baseline gap-2">
                                    <h3 className="text-sm font-semibold">
                                        {plan.goal ?? "Untitled plan"}
                                    </h3>
                                    <span className="rounded bg-indigo-100 px-1.5 py-0.5 text-[10px] text-indigo-900 dark:bg-indigo-900/40 dark:text-indigo-100">
                                        {plan.domain}
                                    </span>
                                    <span className="text-xs text-slate-500">
                                        {done}/{live.length} done
                                    </span>
                                </div>
                                <p className="mt-1 text-xs text-slate-500">
                                    Created {new Date(plan.created_at).toLocaleString()}
                                </p>
                            </Link>
                        </li>
                    );
                })}
            </ul>
        </div>
    );
}
