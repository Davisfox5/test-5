"use client";

import { useState } from "react";

import {
    KbIntegrationGap,
    useKbIntegrationGaps,
    useReevaluateKbIntegrationGaps,
} from "@/lib/kb-integration-gaps";

const SEVERITY_LABEL: Record<string, string> = {
    must: "Must",
    should: "Should",
    may: "May",
};

const SEVERITY_CLASS: Record<string, string> = {
    must: "bg-rose-100 text-rose-800 dark:bg-rose-900/40 dark:text-rose-200",
    should: "bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-200",
    may: "bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300",
};

/**
 * Admin-only: KB-integration alignment report.
 *
 * Lists procedures whose required_integrations point at providers
 * that aren't connected. Drives the "connect or revise" decision.
 * Refreshes when the admin connects/disconnects a provider (the
 * backend auto-re-evaluates on those events; this UI just invalidates
 * the cache on the manual recompute button).
 */
export function KbIntegrationGapsReport() {
    const [providerFilter, setProviderFilter] = useState<string | undefined>(undefined);
    const { data, isLoading, error } = useKbIntegrationGaps({ provider: providerFilter });
    const reevaluate = useReevaluateKbIntegrationGaps();

    const providerSummaries = data?.by_provider ?? {};
    const providers = Object.keys(providerSummaries).sort();

    return (
        <section className="space-y-3">
            <div className="flex flex-wrap items-baseline justify-between gap-2">
                <div>
                    <h2 className="text-base font-semibold">
                        KB-integration alignment
                    </h2>
                    <p className="text-xs text-slate-600 dark:text-slate-300">
                        Procedures in your knowledge base that reference
                        integrations you haven't connected. The action plan
                        synthesizer won't suggest steps targeting unconnected
                        providers. these need either a connection or a
                        procedure revision.
                    </p>
                </div>
                <button
                    type="button"
                    onClick={() => reevaluate.mutate()}
                    disabled={reevaluate.isPending}
                    className="rounded border border-slate-300 px-2 py-1 text-xs disabled:opacity-60 dark:border-slate-600"
                >
                    {reevaluate.isPending ? "Re-evaluating…" : "Re-evaluate now"}
                </button>
            </div>

            {isLoading && <p className="text-sm text-slate-500">Loading alignment report…</p>}
            {error && (
                <p className="text-sm text-rose-600">
                    Failed to load alignment report.
                </p>
            )}

            {data && data.total === 0 && (
                <div className="rounded border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-800 dark:border-emerald-800 dark:bg-emerald-900/30 dark:text-emerald-200">
                    Every procedure in your knowledge base is fully actionable
                    with the integrations you have connected.
                </div>
            )}

            {data && data.total > 0 && (
                <>
                    <div className="flex flex-wrap gap-1">
                        <button
                            type="button"
                            onClick={() => setProviderFilter(undefined)}
                            className={[
                                "rounded px-2 py-0.5 text-xs",
                                !providerFilter
                                    ? "bg-indigo-100 text-indigo-900 dark:bg-indigo-900/40 dark:text-indigo-100"
                                    : "bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300",
                            ].join(" ")}
                        >
                            All ({data.total})
                        </button>
                        {providers.map((p) => (
                            <button
                                key={p}
                                type="button"
                                onClick={() => setProviderFilter(p)}
                                className={[
                                    "rounded px-2 py-0.5 text-xs",
                                    providerFilter === p
                                        ? "bg-indigo-100 text-indigo-900 dark:bg-indigo-900/40 dark:text-indigo-100"
                                        : "bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300",
                                ].join(" ")}
                            >
                                {p} ({providerSummaries[p]})
                            </button>
                        ))}
                    </div>

                    <ul className="space-y-1">
                        {data.items.map((row) => (
                            <GapRow key={row.id} row={row} />
                        ))}
                    </ul>
                </>
            )}

            {reevaluate.data && (
                <p className="text-[11px] text-slate-500">
                    Last re-evaluation: {reevaluate.data.cleared} gaps cleared,{" "}
                    {reevaluate.data.added} added.
                </p>
            )}
        </section>
    );
}

function GapRow({ row }: { row: KbIntegrationGap }) {
    return (
        <li className="rounded border border-slate-200 p-2 text-xs dark:border-slate-700">
            <div className="flex items-center justify-between gap-2">
                <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                        <span
                            className={`rounded px-1.5 py-0.5 text-[10px] uppercase ${SEVERITY_CLASS[row.compliance_level]}`}
                        >
                            {SEVERITY_LABEL[row.compliance_level] ?? row.compliance_level}
                        </span>
                        <span className="rounded bg-slate-200 px-1.5 py-0.5 font-mono text-[10px] dark:bg-slate-800">
                            {row.required_provider}
                            {row.operation ? `.${row.operation}` : ""}
                        </span>
                        <span className="truncate font-medium">
                            {row.procedure_title || row.doc_title || "(untitled procedure)"}
                        </span>
                    </div>
                    {row.doc_title && row.procedure_title && (
                        <p className="mt-0.5 text-[10px] text-slate-500">
                            From {row.doc_title}
                        </p>
                    )}
                </div>
                <span className="shrink-0 text-[10px] text-slate-500">
                    {new Date(row.detected_at).toLocaleDateString()}
                </span>
            </div>
        </li>
    );
}
