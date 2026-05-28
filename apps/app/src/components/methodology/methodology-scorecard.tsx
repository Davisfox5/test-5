"use client";

import Link from "next/link";
import { useState } from "react";
import type { MethodologyCoverage } from "@/lib/interactions";

/**
 * Methodology Scorecard — 4-quadrant card showing which stages of
 * the rep's playbook the call covered.
 *
 * Defaults to a 2x2 grid for SPIN (Situation / Problem / Implication /
 * Need-payoff). Falls back to a flat list when the framework defines
 * a different number of stages (MEDDIC has 6, structured-resolution
 * has 4 different ones, etc).
 */
export function MethodologyScorecard({
    coverage,
}: {
    coverage: MethodologyCoverage | undefined;
}) {
    if (!coverage || coverage.framework === "none") return null;

    const stagesAll = [...coverage.covered, ...coverage.missing];
    const stages = framework_stage_order(coverage.framework, stagesAll);

    // SPIN renders cleanly as a 2x2; everything else as a flat grid.
    const isSpin = coverage.framework.toLowerCase() === "spin";

    return (
        <section className="rounded-lg border border-border bg-bg-card p-5">
            <header className="mb-3 flex items-baseline justify-between gap-2">
                <h3 className="text-sm font-semibold capitalize">
                    {coverage.framework} coverage
                </h3>
                <span className="text-xs text-text-subtle">
                    {coverage.covered.length}/{stages.length} stages
                </span>
            </header>
            <div
                className={`grid gap-2 ${
                    isSpin ? "grid-cols-2" : "grid-cols-1 sm:grid-cols-2 lg:grid-cols-3"
                }`}
            >
                {stages.map((stage) => {
                    const covered = coverage.covered.includes(stage);
                    return (
                        <div
                            key={stage}
                            className={`rounded-md border p-3 text-sm ${
                                covered
                                    ? "border-accent-emerald/40 bg-accent-emerald/10"
                                    : "border-border-light bg-bg-secondary"
                            }`}
                        >
                            <div className="flex items-center gap-2">
                                <span
                                    aria-hidden
                                    className={
                                        covered
                                            ? "text-accent-emerald"
                                            : "text-text-subtle"
                                    }
                                >
                                    {covered ? "●" : "○"}
                                </span>
                                <span
                                    className={`font-medium capitalize ${
                                        covered ? "text-text" : "text-text-muted"
                                    }`}
                                >
                                    {stage.replace(/_/g, " ")}
                                </span>
                            </div>
                        </div>
                    );
                })}
            </div>
            {coverage.next_question && (
                <NextQuestionCallout question={coverage.next_question} />
            )}
        </section>
    );
}

function NextQuestionCallout({ question }: { question: string }) {
    const [copied, setCopied] = useState(false);
    async function copy() {
        try {
            await navigator.clipboard.writeText(question);
            setCopied(true);
            // Reset the chip after a beat so the rep can copy again on
            // a longer call. 1.5s is short enough to feel responsive
            // and long enough to read the confirmation.
            setTimeout(() => setCopied(false), 1500);
        } catch {
            // Clipboard can be blocked by Permissions-Policy on some
            // tenants' deployments. Silent — the action-items link
            // below still gives the rep a path.
        }
    }
    return (
        <div className="mt-3 rounded-md border border-primary-soft bg-primary-soft/30 p-3 text-sm">
            <div className="mb-1 flex items-baseline justify-between gap-2">
                <span className="text-xs font-semibold uppercase tracking-wide text-text-muted">
                    Suggested next question
                </span>
                <div className="flex gap-2">
                    <button
                        type="button"
                        onClick={copy}
                        className="rounded border border-border bg-bg-card px-2 py-0.5 text-[11px] text-text hover:bg-card-hover"
                    >
                        {copied ? "Copied ✓" : "Copy"}
                    </button>
                    <Link
                        href="/action-plans"
                        className="rounded border border-border bg-bg-card px-2 py-0.5 text-[11px] text-primary hover:bg-card-hover"
                    >
                        Open action plans
                    </Link>
                </div>
            </div>
            <div className="text-text">{question}</div>
        </div>
    );
}

// Order stages canonically per framework so the layout is consistent
// across calls. Falls back to whatever order the LLM emitted when the
// framework isn't recognized.
function framework_stage_order(framework: string, raw: string[]): string[] {
    const normalized = framework.toLowerCase();
    const known: Record<string, string[]> = {
        spin: ["situation", "problem", "implication", "need_payoff"],
        meddic: ["metrics", "economic_buyer", "decision_criteria", "decision_process", "identify_pain", "champion"],
        structured_resolution: ["acknowledge", "diagnose", "resolve", "confirm"],
    };
    const canonical = known[normalized];
    if (!canonical) return raw;

    // Match LLM-emitted stages to canonical names by lowercase
    // substring — robust to "Need-payoff" vs "need_payoff" vs
    // "needPayoff" emissions.
    const result: string[] = [];
    const seen = new Set<string>();
    for (const stage of canonical) {
        const match = raw.find(
            (r) =>
                r.toLowerCase().replace(/[\s_-]+/g, "_") ===
                stage.replace(/[\s_-]+/g, "_"),
        );
        if (match) {
            result.push(match);
            seen.add(match);
        } else {
            // Render canonical name even when the LLM didn't emit it,
            // so the rep sees the full framework's shape with the
            // missing stage clearly absent from `covered`.
            result.push(stage);
        }
    }
    // Append any LLM-emitted stages we didn't recognize at the end.
    for (const r of raw) {
        if (!seen.has(r)) result.push(r);
    }
    return result;
}
