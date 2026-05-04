"use client";

/** Layout 2 — Dossier (default landing).
 *
 * Overview header at the top, two-column body (Contacts left, Recent
 * Interactions right), Open Action Items below. Most familiar to
 * anyone coming from Salesforce/HubSpot.
 */

import type { CustomerDetail } from "@/lib/customers";
import {
    ActionItemsCard,
    CommitmentsCard,
    ContactsCard,
    InteractionsCard,
    OverviewHeader,
    WarningsCard,
} from "./shared";

export function Layout2Dossier({ c }: { c: CustomerDetail }) {
    return (
        <div className="space-y-6">
            <OverviewHeader c={c} />

            <WarningsCard c={c} />

            <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
                <div className="lg:col-span-1 space-y-6">
                    <ContactsCard c={c} />
                    <CommitmentsCard c={c} />
                </div>
                <div className="lg:col-span-2">
                    <InteractionsCard c={c} />
                </div>
            </div>

            <ActionItemsCard c={c} />
        </div>
    );
}
