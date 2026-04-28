"use client";

import {
    OAuthProvider,
    authorizeUrlFor,
    useOAuthStatus,
    useRevokeIntegration,
} from "@/lib/oauth";
import { humanizeError } from "@/components/admin/section";

interface ProviderSpec {
    key: OAuthProvider;
    label: string;
    blurb: string;
}

// Google + Microsoft cover Drive/OneDrive for storage and email/calendar.
const PROVIDERS: ProviderSpec[] = [
    {
        key: "pipedrive",
        label: "Pipedrive",
        blurb: "Sync deals, contacts, and write back call summaries.",
    },
    {
        key: "hubspot",
        label: "HubSpot",
        blurb: "Pull contacts and companies from your hub.",
    },
    {
        key: "salesforce",
        label: "Salesforce",
        blurb: "Pull accounts and opportunities from your org.",
    },
    {
        key: "google",
        label: "Google Drive + Workspace",
        blurb: "Drive ingest, Gmail send, and Calendar events.",
    },
    {
        key: "microsoft",
        label: "Microsoft 365 (OneDrive)",
        blurb: "OneDrive ingest, Outlook send, and Calendar events.",
    },
];

export function IntegrationsSection() {
    const { data, isLoading, error } = useOAuthStatus();
    const revoke = useRevokeIntegration();
    const connected = new Map<string, { id: string; created_at: string }>(
        (data?.integrations ?? []).map((i) => [
            i.provider,
            { id: i.id, created_at: i.created_at },
        ]),
    );

    if (isLoading) {
        return (
            <p className="text-sm text-text-muted">Loading integrations…</p>
        );
    }
    if (error) {
        return (
            <p className="text-sm text-accent-rose">
                {humanizeError(error)}
            </p>
        );
    }

    return (
        <ul className="space-y-2">
            {PROVIDERS.map((p) => {
                const conn = connected.get(p.key);
                return (
                    <li
                        key={p.key}
                        className="flex flex-wrap items-start justify-between gap-3 rounded-md border border-border bg-bg-raised p-3"
                    >
                        <div className="min-w-0 flex-1">
                            <div className="flex items-center gap-2">
                                <span className="font-medium">{p.label}</span>
                                {conn ? (
                                    <span className="text-xs uppercase tracking-wide text-accent-emerald">
                                        Connected
                                    </span>
                                ) : null}
                            </div>
                            <p className="mt-0.5 text-xs text-text-muted">
                                {p.blurb}
                            </p>
                            {conn ? (
                                <p className="mt-1 text-xs text-text-subtle">
                                    Linked{" "}
                                    {new Date(
                                        conn.created_at,
                                    ).toLocaleDateString()}
                                </p>
                            ) : null}
                        </div>
                        <div className="flex gap-2">
                            {conn ? (
                                <button
                                    type="button"
                                    onClick={() => {
                                        if (
                                            confirm(
                                                `Disconnect ${p.label}? Existing tokens will be revoked.`,
                                            )
                                        )
                                            revoke.mutate(p.key);
                                    }}
                                    disabled={revoke.isPending}
                                    className="rounded-md border border-border bg-bg-card px-3 py-1.5 text-xs text-accent-rose disabled:opacity-50"
                                >
                                    Disconnect
                                </button>
                            ) : (
                                <a
                                    href={authorizeUrlFor(p.key)}
                                    target="_blank"
                                    rel="noopener noreferrer"
                                    className="rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-white"
                                >
                                    Connect
                                </a>
                            )}
                        </div>
                    </li>
                );
            })}
            {revoke.isError ? (
                <p className="text-xs text-accent-rose">
                    {humanizeError(revoke.error)}
                </p>
            ) : null}
        </ul>
    );
}
