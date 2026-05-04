"use client";

/** Layout 1 — Story.
 *
 * Single-column chronological feed. Each interaction is its own card
 * with the AI summary expanded for the most recent one. Contacts and
 * action items live in collapsible sections at the top so the story
 * is the dominant signal.
 */

import { useState } from "react";
import type { CustomerDetail } from "@/lib/customers";
import {
    ActionItemsCard,
    CommitmentsCard,
    ContactsCard,
    InteractionsCard,
    OverviewHeader,
    WarningsCard,
} from "./shared";

export function Layout1Story({ c }: { c: CustomerDetail }) {
    const [showContacts, setShowContacts] = useState(false);
    const [showActions, setShowActions] = useState(false);
    const [showCommitments, setShowCommitments] = useState(false);
    return (
        <div className="space-y-6">
            <OverviewHeader c={c} />

            <WarningsCard c={c} />

            <div className="flex flex-wrap gap-3 text-xs text-text-muted">
                <button
                    type="button"
                    onClick={() => setShowContacts((v) => !v)}
                    className="rounded-md border border-border px-3 py-1.5 hover:bg-bg-card-hover"
                >
                    {showContacts ? "Hide" : "Show"} contacts (
                    {c.contacts.length})
                </button>
                <button
                    type="button"
                    onClick={() => setShowCommitments((v) => !v)}
                    className="rounded-md border border-border px-3 py-1.5 hover:bg-bg-card-hover"
                >
                    {showCommitments ? "Hide" : "Show"} commitments (
                    {c.commitments.filter(
                        (x) => x.status === "pending" || x.status === "overdue",
                    ).length}
                    )
                </button>
                <button
                    type="button"
                    onClick={() => setShowActions((v) => !v)}
                    className="rounded-md border border-border px-3 py-1.5 hover:bg-bg-card-hover"
                >
                    {showActions ? "Hide" : "Show"} open action items (
                    {c.open_action_items.length})
                </button>
            </div>

            {showContacts ? <ContactsCard c={c} /> : null}
            {showCommitments ? <CommitmentsCard c={c} /> : null}
            {showActions ? <ActionItemsCard c={c} /> : null}

            <InteractionsCard
                c={c}
                title="The story (newest first)"
                expandFirst
            />
        </div>
    );
}
