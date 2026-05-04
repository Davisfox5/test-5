"use client";

/** Layout 4 — Compact.
 *
 * Single dense scroll with collapsible sections. Everything's
 * available without tabs; the user expands what they need. Tradeoff:
 * no tab cost, busier page.
 */

import { useState, type ReactNode } from "react";
import type { CustomerDetail } from "@/lib/customers";
import {
    ActionItemsCard,
    ContactsCard,
    InteractionsCard,
    OverviewHeader,
} from "./shared";

export function Layout4Compact({ c }: { c: CustomerDetail }) {
    return (
        <div className="space-y-3">
            <OverviewHeader c={c} compact />
            <Collapsible
                title={`Contacts (${c.contacts.length})`}
                defaultOpen
            >
                <ContactsCard c={c} />
            </Collapsible>
            <Collapsible
                title={`Recent interactions (${c.recent_interactions.length})`}
                defaultOpen
            >
                <InteractionsCard c={c} title="Interactions" />
            </Collapsible>
            <Collapsible
                title={`Open action items (${c.open_action_items.length})`}
            >
                <ActionItemsCard c={c} />
            </Collapsible>
        </div>
    );
}

function Collapsible({
    title,
    defaultOpen = false,
    children,
}: {
    title: string;
    defaultOpen?: boolean;
    children: ReactNode;
}) {
    const [open, setOpen] = useState(defaultOpen);
    return (
        <div className="rounded-lg border border-border bg-bg-card">
            <button
                type="button"
                onClick={() => setOpen((v) => !v)}
                className="flex w-full items-center justify-between px-5 py-3 text-left text-sm font-semibold hover:bg-bg-card-hover"
            >
                <span>{title}</span>
                <span className="text-text-subtle">{open ? "−" : "+"}</span>
            </button>
            {open ? <div className="border-t border-border">{children}</div> : null}
        </div>
    );
}
