"use client";

import Link from "next/link";
import { use } from "react";

import { useScorecard } from "@/lib/scorecards";

import { ScorecardEditor } from "../_editor";

export default function ScorecardDetailPage({
    params,
}: {
    params: Promise<{ id: string }>;
}) {
    const { id } = use(params);
    const { data, isLoading, error } = useScorecard(id);

    if (isLoading) {
        return (
            <div className="rounded-lg border border-border bg-bg-card p-6 animate-pulse">
                <div className="h-5 w-48 bg-bg-secondary rounded mb-3" />
                <div className="h-3 w-2/3 bg-bg-secondary rounded" />
            </div>
        );
    }

    if (error || !data) {
        return (
            <div className="space-y-3">
                <Link
                    href="/scorecards"
                    className="text-sm text-text-subtle hover:text-text-muted"
                >
                    ← Back to scorecards
                </Link>
                <div className="rounded-lg border border-accent-rose bg-bg-card p-4 text-sm text-accent-rose">
                    Couldn&apos;t load scorecard: {(error as Error)?.message ?? "not found"}
                </div>
            </div>
        );
    }

    return <ScorecardEditor mode="edit" initial={data} />;
}
