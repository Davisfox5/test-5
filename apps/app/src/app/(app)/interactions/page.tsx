"use client";

/**
 * Legacy /interactions list — redirects to ``/customers?tab=all-interactions``.
 *
 * The global Interactions feed has been demoted from the sidebar and
 * lives as a sibling tab on /customers per the redesign plan. This
 * stub keeps old shared/bookmarked links and the audit log working.
 *
 * /interactions/{id} detail pages are unchanged and continue to live
 * under [id]/page.tsx.
 */

import { useEffect } from "react";
import { useRouter } from "next/navigation";

export default function InteractionsListRedirect() {
    const router = useRouter();
    useEffect(() => {
        router.replace("/customers?tab=all-interactions");
    }, [router]);
    return (
        <div className="flex h-64 items-center justify-center text-sm text-text-muted">
            Redirecting to Customers › All Interactions…
        </div>
    );
}
