"use client";

import { useState } from "react";
import {
    OAuthProvider,
    useOAuthStatus,
    useRevokeIntegration,
    useStartOAuth,
} from "@/lib/oauth";
import {
    CrmProvider,
    CrmSyncLog,
    useCrmSyncLogs,
    useTriggerCrmSync,
} from "@/lib/crm";
import { useBackfillJob, useStartBackfill } from "@/lib/backfill";
import { humanizeError } from "@/components/admin/section";

// Mailbox providers that support a historical "Sync last 90 days" backfill.
const BACKFILL_PROVIDER_KEYS = new Set<string>(["google", "microsoft"]);

interface ProviderSpec {
    key: OAuthProvider;
    label: string;
    blurb: string;
    // True when the provider is a CRM (drives whether we render the
    // sync-now controls). Backend's CRM service supports
    // pipedrive / hubspot / salesforce.
    isCrm?: boolean;
}

// Google + Microsoft cover Drive/OneDrive for storage and email/calendar.
const PROVIDERS: ProviderSpec[] = [
    {
        key: "pipedrive",
        label: "Pipedrive",
        blurb: "Sync deals, contacts, and write back call summaries.",
        isCrm: true,
    },
    {
        key: "hubspot",
        label: "HubSpot",
        blurb: "Pull contacts and companies from your hub.",
        isCrm: true,
    },
    {
        key: "salesforce",
        label: "Salesforce",
        blurb: "Pull accounts and opportunities from your org.",
        isCrm: true,
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

const CRM_PROVIDER_KEYS = new Set<string>(
    PROVIDERS.filter((p) => p.isCrm).map((p) => p.key),
);

export function IntegrationsSection() {
    const { data, isLoading, error } = useOAuthStatus();
    const revoke = useRevokeIntegration();
    const startOAuth = useStartOAuth();
    const triggerSync = useTriggerCrmSync();
    // Pull all CRM sync logs for the tenant in one shot; we filter
    // client-side per-provider so the connected-CRM cards each render
    // their own last-five history without N round-trips.
    const { data: syncLogs } = useCrmSyncLogs(undefined, 50);

    const connected = new Map<string, { id: string; created_at: string }>(
        (data?.integrations ?? []).map((i) => [
            i.provider,
            { id: i.id, created_at: i.created_at },
        ]),
    );

    const handleConnect = async (provider: OAuthProvider) => {
        try {
            const url = await startOAuth.mutateAsync(provider);
            // Full-page redirect — the provider's consent screen will
            // eventually redirect back to the API callback, which redirects
            // the user back to the SPA settings page.
            window.location.assign(url);
        } catch {
            // The error renders below via startOAuth.isError.
        }
    };

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
                const showSync = p.isCrm && conn;
                const showBackfill = !!conn && BACKFILL_PROVIDER_KEYS.has(p.key);
                const providerLogs = (syncLogs ?? []).filter(
                    (log) => log.provider === p.key,
                );
                return (
                    <li
                        key={p.key}
                        className="rounded-md border border-border bg-bg-raised p-3"
                    >
                        <div className="flex flex-wrap items-start justify-between gap-3">
                            <div className="min-w-0 flex-1">
                                <div className="flex items-center gap-2">
                                    <span className="font-medium">
                                        {p.label}
                                    </span>
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
                                    <button
                                        type="button"
                                        onClick={() => handleConnect(p.key)}
                                        disabled={startOAuth.isPending}
                                        className="rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-white disabled:opacity-50"
                                    >
                                        {startOAuth.isPending &&
                                        startOAuth.variables === p.key
                                            ? "Connecting…"
                                            : "Connect"}
                                    </button>
                                )}
                            </div>
                        </div>

                        {showSync ? (
                            <CrmSyncCard
                                provider={p.key as CrmProvider}
                                providerLabel={p.label}
                                logs={providerLogs}
                                onSyncNow={() =>
                                    triggerSync.mutate(p.key as CrmProvider)
                                }
                                syncing={
                                    triggerSync.isPending &&
                                    triggerSync.variables === p.key
                                }
                                lastError={
                                    triggerSync.isError &&
                                    triggerSync.variables === p.key
                                        ? humanizeError(triggerSync.error)
                                        : null
                                }
                            />
                        ) : null}

                        {showBackfill ? (
                            <MailboxBackfillCard provider={p.key} />
                        ) : null}
                    </li>
                );
            })}
            {revoke.isError ? (
                <p className="text-xs text-accent-rose">
                    {humanizeError(revoke.error)}
                </p>
            ) : null}
            {startOAuth.isError ? (
                <p className="text-xs text-accent-rose">
                    {humanizeError(startOAuth.error)}
                </p>
            ) : null}
        </ul>
    );
}

/* ── Mailbox backfill card ──────────────────────────────────────────── */

// "Sync last 90 days" — pulls historical mail through the same ingest
// pipeline as live mail. Connecting a mailbox is forward-only, so this is
// the only way to analyze conversations that predate the connect.
function MailboxBackfillCard({ provider }: { provider: OAuthProvider }) {
    const start = useStartBackfill();
    const [jobId, setJobId] = useState<string | null>(null);
    const [notice, setNotice] = useState<string | null>(null);
    const { data: job } = useBackfillJob(jobId);

    const inFlight =
        start.isPending ||
        job?.status === "queued" ||
        job?.status === "running";

    const handleSync = async () => {
        setNotice(null);
        try {
            // If a sync is already running (another tab, double-click), the
            // API returns that job's handle — polling just resumes on it.
            const res = await start.mutateAsync({ provider });
            setJobId(res.job_id);
        } catch (e) {
            setNotice(humanizeError(e));
        }
    };

    let statusLine: string | null = null;
    if (inFlight) {
        statusLine = `Syncing… ${job?.ingested ?? 0} imported`;
    } else if (job?.status === "done") {
        statusLine = `Imported ${job.ingested} message${
            job.ingested === 1 ? "" : "s"
        }${job.skipped ? ` · ${job.skipped} already synced` : ""}.${
            job.error ? ` ${job.error}` : ""
        }`;
    } else if (job?.status === "error") {
        statusLine = job.error
            ? `Sync failed: ${job.error}`
            : "Sync failed.";
    }

    return (
        <div className="mt-3 rounded-md border border-border bg-bg-card p-3">
            <div className="flex flex-wrap items-center justify-between gap-3">
                <div className="min-w-0 text-xs text-text-muted">
                    <div className="text-text-main">Historical mail</div>
                    <p className="mt-0.5">
                        Import the last 90 days so past conversations are
                        analyzed too.
                    </p>
                </div>
                <button
                    type="button"
                    onClick={handleSync}
                    disabled={inFlight}
                    className="rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-white disabled:opacity-50"
                >
                    {inFlight
                        ? "Syncing…"
                        : job?.status === "done"
                          ? "Sync again"
                          : "Sync last 90 days"}
                </button>
            </div>
            {statusLine ? (
                <p
                    className={`mt-2 text-xs ${
                        job?.status === "error"
                            ? "text-accent-rose"
                            : "text-text-muted"
                    }`}
                >
                    {statusLine}
                </p>
            ) : null}
            {notice ? (
                <p className="mt-2 text-xs text-accent-amber">{notice}</p>
            ) : null}
        </div>
    );
}

/* ── CRM sync card ──────────────────────────────────────────────────── */

function CrmSyncCard({
    provider,
    providerLabel,
    logs,
    onSyncNow,
    syncing,
    lastError,
}: {
    provider: CrmProvider;
    providerLabel: string;
    logs: CrmSyncLog[];
    onSyncNow: () => void;
    syncing: boolean;
    lastError: string | null;
}) {
    // Suppress the unused-var lint for the prop we accept for typing
    // hygiene; provider isn't read inline because the hook upstream
    // already feeds the right rows.
    void provider;

    const lastRun = logs[0];
    const recent = logs.slice(0, 5);

    return (
        <div className="mt-3 rounded-md border border-border bg-bg-card p-3">
            <div className="flex flex-wrap items-center justify-between gap-3">
                <div className="text-xs text-text-muted">
                    <div>
                        Last synced:{" "}
                        <span className="text-text-main">
                            {lastRun?.finished_at
                                ? new Date(lastRun.finished_at).toLocaleString()
                                : lastRun?.started_at
                                  ? new Date(lastRun.started_at).toLocaleString()
                                  : "Never"}
                        </span>
                    </div>
                    <div className="mt-0.5">
                        Status:{" "}
                        <StatusBadge status={lastRun?.status} />
                    </div>
                </div>
                <button
                    type="button"
                    onClick={onSyncNow}
                    disabled={syncing}
                    className="rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-white disabled:opacity-50"
                >
                    {syncing ? "Syncing…" : "Sync now"}
                </button>
            </div>
            {lastError ? (
                <p className="mt-2 text-xs text-accent-rose">{lastError}</p>
            ) : null}
            {lastRun?.error ? (
                <p className="mt-2 text-xs text-accent-rose">
                    Last sync error: {lastRun.error}
                </p>
            ) : null}

            <div className="mt-3">
                <h5 className="text-xs font-semibold uppercase tracking-wide text-text-subtle">
                    Recent runs
                </h5>
                {recent.length === 0 ? (
                    <p className="mt-1 text-xs text-text-subtle">
                        No sync runs yet for {providerLabel}.
                    </p>
                ) : (
                    <table className="mt-1 w-full text-xs">
                        <thead className="text-text-subtle">
                            <tr className="text-left">
                                <th className="py-1">Started</th>
                                <th className="py-1">Status</th>
                                <th className="py-1">Customers</th>
                                <th className="py-1">Contacts</th>
                                <th className="py-1">Briefs</th>
                            </tr>
                        </thead>
                        <tbody>
                            {recent.map((log) => (
                                <tr
                                    key={log.id}
                                    className="border-t border-border align-middle"
                                >
                                    <td className="py-1 text-text-muted">
                                        {new Date(
                                            log.started_at,
                                        ).toLocaleString()}
                                    </td>
                                    <td className="py-1">
                                        <StatusBadge status={log.status} />
                                    </td>
                                    <td className="py-1">
                                        {log.customers_upserted}
                                    </td>
                                    <td className="py-1">
                                        {log.contacts_upserted}
                                    </td>
                                    <td className="py-1">
                                        {log.briefs_rebuilt}
                                    </td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                )}
            </div>
        </div>
    );
}

function StatusBadge({ status }: { status: string | undefined }) {
    if (!status) return <span className="text-text-subtle">-</span>;
    const tone =
        status === "success"
            ? "text-accent-emerald"
            : status === "failed"
              ? "text-accent-rose"
              : status === "running" || status === "scheduled"
                ? "text-accent-amber"
                : "text-text-muted";
    return <span className={`capitalize ${tone}`}>{status}</span>;
}
