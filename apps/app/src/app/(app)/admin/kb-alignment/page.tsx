"use client";

import { KbIntegrationGapsReport } from "@/components/admin/kb-integration-gaps";

export default function AdminKbAlignmentPage() {
    return (
        <div className="mx-auto max-w-4xl space-y-4 p-4">
            <KbIntegrationGapsReport />
        </div>
    );
}
