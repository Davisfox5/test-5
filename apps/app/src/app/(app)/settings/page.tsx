"use client";

import { useMe } from "@/lib/me";

export default function SettingsPage() {
    const { data } = useMe();
    return (
        <div className="space-y-4">
            <header>
                <h2 className="text-2xl font-bold">Settings</h2>
                <p className="text-text-muted mt-1">
                    Workspace preferences for {data?.tenant.name ?? "your tenant"}.
                </p>
            </header>
            <div className="rounded-lg border border-border border-dashed bg-bg-card p-8 text-center text-text-subtle">
                Tenant + user settings will land here. Executive-role surfaces
                (branding, custom domains, API keys, billing) gate behind
                <code className="ml-1 text-text-muted">useRole() === &quot;executive&quot;</code>.
            </div>
        </div>
    );
}
