"use client";

/** Layout 3 — Sections.
 *
 * Three nested tabs below the header: Overview / Interactions /
 * Action Items. Each tab is dense and focused. Forces the user to
 * pick a lens.
 */

import { useState } from "react";
import type { CustomerDetail } from "@/lib/customers";
import {
    ActionItemsCard,
    ContactsCard,
    InteractionsCard,
    OverviewHeader,
} from "./shared";

type SubTab = "overview" | "interactions" | "actions";

export function Layout3Sections({ c }: { c: CustomerDetail }) {
    const [tab, setTab] = useState<SubTab>("overview");
    return (
        <div className="space-y-6">
            <OverviewHeader c={c} />

            <div className="flex gap-2 border-b border-border">
                <SubTabButton
                    active={tab === "overview"}
                    onClick={() => setTab("overview")}
                    label="Overview"
                    count={c.contacts.length}
                />
                <SubTabButton
                    active={tab === "interactions"}
                    onClick={() => setTab("interactions")}
                    label="Interactions"
                    count={c.recent_interactions.length}
                />
                <SubTabButton
                    active={tab === "actions"}
                    onClick={() => setTab("actions")}
                    label="Action Items"
                    count={c.open_action_items.length}
                />
            </div>

            {tab === "overview" ? <ContactsCard c={c} /> : null}
            {tab === "interactions" ? <InteractionsCard c={c} /> : null}
            {tab === "actions" ? <ActionItemsCard c={c} /> : null}
        </div>
    );
}

function SubTabButton({
    active,
    onClick,
    label,
    count,
}: {
    active: boolean;
    onClick: () => void;
    label: string;
    count: number;
}) {
    return (
        <button
            type="button"
            onClick={onClick}
            className={`-mb-px border-b-2 px-4 py-2 text-sm transition-colors ${
                active
                    ? "border-primary text-primary"
                    : "border-transparent text-text-muted hover:text-text"
            }`}
        >
            {label}
            <span className="ml-2 text-xs text-text-subtle">({count})</span>
        </button>
    );
}
