"use client";

/**
 * Customer detail page — Phase 3C wraps Phase 3B.
 *
 * Four numbered layouts behind a tab switcher at the top. Layout 2
 * (Dossier) is the default landing per plan; the other three are
 * variants the user picks between by trying them. Per the user's
 * answer in the planning round: "tabbed navigation across the top —
 * this is mostly just for testing." So no preview popover; just
 * named-by-number tabs that swap the body component.
 *
 * One detail fetch feeds all four layouts — they each consume
 * ``CustomerDetail`` from /lib/customers.
 */

import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";
import { useCustomerDetail } from "@/lib/customers";
import { Layout1Story } from "./_layouts/layout1-story";
import { Layout2Dossier } from "./_layouts/layout2-dossier";
import { Layout3Sections } from "./_layouts/layout3-sections";
import { Layout4Compact } from "./_layouts/layout4-compact";

type LayoutKey = "1" | "2" | "3" | "4";

const LAYOUT_DESCRIPTIONS: Record<LayoutKey, string> = {
    "1": "Story — chronological feed",
    "2": "Dossier — overview + drilldown (default)",
    "3": "Sections — Overview / Interactions / Action Items tabs",
    "4": "Compact — single scroll, collapsible",
};

export default function CustomerDetailPage() {
    const params = useParams<{ id: string }>();
    const id = params?.id;
    const detail = useCustomerDetail(id);
    const [layout, setLayout] = useState<LayoutKey>("2");

    if (!id) return null;

    if (detail.isLoading) {
        return (
            <div className="space-y-4">
                <div className="h-8 w-1/3 animate-pulse rounded bg-bg-card-hover" />
                <div className="h-4 w-1/2 animate-pulse rounded bg-bg-card-hover" />
                <div className="h-64 animate-pulse rounded-lg bg-bg-card" />
            </div>
        );
    }

    if (detail.error || !detail.data) {
        return (
            <div className="space-y-3">
                <Link
                    href="/customers"
                    className="text-sm text-primary hover:underline"
                >
                    ← Back to customers
                </Link>
                <p className="text-accent-rose">
                    Couldn&apos;t load this customer.
                </p>
            </div>
        );
    }

    const c = detail.data;

    return (
        <div className="space-y-6">
            <div className="flex items-center justify-between gap-4">
                <Link
                    href="/customers"
                    className="text-sm text-primary hover:underline"
                >
                    ← Back to customers
                </Link>
                <div
                    role="tablist"
                    aria-label="Customer detail layout"
                    className="flex items-center gap-1 rounded-md border border-border bg-bg-secondary p-1"
                >
                    {(["1", "2", "3", "4"] as const).map((k) => (
                        <button
                            key={k}
                            type="button"
                            role="tab"
                            aria-selected={layout === k}
                            onClick={() => setLayout(k)}
                            title={LAYOUT_DESCRIPTIONS[k]}
                            className={`rounded px-3 py-1 text-xs font-medium transition-colors ${
                                layout === k
                                    ? "bg-primary text-white"
                                    : "text-text-muted hover:text-text"
                            }`}
                        >
                            Layout {k}
                        </button>
                    ))}
                </div>
            </div>

            {layout === "1" ? <Layout1Story c={c} /> : null}
            {layout === "2" ? <Layout2Dossier c={c} /> : null}
            {layout === "3" ? <Layout3Sections c={c} /> : null}
            {layout === "4" ? <Layout4Compact c={c} /> : null}
        </div>
    );
}
