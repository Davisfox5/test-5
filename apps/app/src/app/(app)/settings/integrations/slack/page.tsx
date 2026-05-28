"use client";

/**
 * Slack integration settings page.
 *
 * Three states:
 *   1. Not installed → "Connect Slack" button kicks off OAuth.
 *   2. Installed without channel → channel picker.
 *   3. Installed with channel → status display + "Disconnect" button.
 */

import { useEffect, useState } from "react";
import {
    useSlackChannels,
    useSlackInstallUrl,
    useSlackIntegration,
    useSetSlackChannel,
    useUninstallSlack,
    type SlackChannel,
} from "@/lib/slack";

export default function SlackSettingsPage() {
    const { data: integration, isLoading } = useSlackIntegration();
    const install = useSlackInstallUrl();
    const setChannel = useSetSlackChannel();
    const uninstall = useUninstallSlack();
    const channels = useSlackChannels(Boolean(integration && !integration.revoked_at));
    const [selected, setSelected] = useState<string | null>(null);

    useEffect(() => {
        if (integration?.default_channel_id && selected === null) {
            setSelected(integration.default_channel_id);
        }
    }, [integration?.default_channel_id, selected]);

    if (isLoading) {
        return <p className="text-text-muted">Loading Slack integration…</p>;
    }

    return (
        <div className="space-y-6">
            <div>
                <h1 className="text-2xl font-bold">Slack alerts</h1>
                <p className="text-sm text-text-muted">
                    Connect your Slack workspace to receive manager alerts in a channel of your choice.
                    Alerts are severity-gated; the threshold is configurable from the Manager page.
                </p>
            </div>

            {!integration ? (
                <NotConnected
                    onConnect={async () => {
                        const { url } = await install.mutateAsync();
                        if (url) window.location.href = url;
                    }}
                    pending={install.isPending}
                />
            ) : (
                <div className="space-y-4">
                    <ConnectedHeader teamName={integration.slack_team_name} />
                    <ChannelPicker
                        channels={channels.data || []}
                        loading={channels.isLoading}
                        selected={selected ?? integration.default_channel_id ?? null}
                        onChange={(id) => setSelected(id)}
                        onSave={() => {
                            if (!selected) return;
                            const ch = (channels.data || []).find((c: SlackChannel) => c.id === selected);
                            setChannel.mutate({
                                channel_id: selected,
                                channel_name: ch?.name,
                            });
                        }}
                        saving={setChannel.isPending}
                    />
                    <button
                        onClick={() => uninstall.mutate()}
                        disabled={uninstall.isPending}
                        className="rounded border border-error px-3 py-1 text-sm text-error hover:bg-error-soft"
                    >
                        {uninstall.isPending ? "Disconnecting…" : "Disconnect Slack"}
                    </button>
                </div>
            )}
        </div>
    );
}

function NotConnected({ onConnect, pending }: { onConnect: () => void; pending: boolean }) {
    return (
        <div className="rounded-lg border border-border bg-bg-card p-4">
            <p className="text-sm">
                Slack isn't connected for this tenant yet. Connect now to start receiving alerts.
            </p>
            <button
                onClick={onConnect}
                disabled={pending}
                className="mt-3 rounded bg-primary px-4 py-2 text-sm font-semibold text-bg disabled:opacity-50"
            >
                {pending ? "Preparing…" : "Connect Slack"}
            </button>
        </div>
    );
}

function ConnectedHeader({ teamName }: { teamName: string | null }) {
    return (
        <div className="rounded-lg border border-border bg-bg-card p-4">
            <p className="text-sm text-text">
                Connected to <span className="font-semibold">{teamName || "your workspace"}</span>.
            </p>
        </div>
    );
}

function ChannelPicker({
    channels,
    loading,
    selected,
    onChange,
    onSave,
    saving,
}: {
    channels: SlackChannel[];
    loading: boolean;
    selected: string | null;
    onChange: (id: string) => void;
    onSave: () => void;
    saving: boolean;
}) {
    return (
        <div className="rounded-lg border border-border bg-bg-card p-4">
            <label className="block text-sm font-medium text-text">Default alert channel</label>
            <p className="mt-1 text-xs text-text-muted">
                The Slack bot must be invited to the channel before it can post.
            </p>
            <select
                value={selected || ""}
                onChange={(e) => onChange(e.target.value)}
                disabled={loading}
                className="mt-2 w-full rounded border border-border bg-bg px-2 py-1 text-sm"
            >
                <option value="">Select a channel…</option>
                {channels.map((c) => (
                    <option key={c.id} value={c.id}>
                        {c.is_private ? "🔒 " : "#"}
                        {c.name}
                    </option>
                ))}
            </select>
            <button
                onClick={onSave}
                disabled={!selected || saving}
                className="mt-3 rounded bg-primary px-3 py-1 text-sm font-semibold text-bg disabled:opacity-50"
            >
                {saving ? "Saving…" : "Save channel"}
            </button>
        </div>
    );
}
