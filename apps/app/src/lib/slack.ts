"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useApi } from "./api";

export interface SlackIntegration {
    tenant_id: string;
    slack_team_id: string;
    slack_team_name: string | null;
    default_channel_id: string | null;
    default_channel_name: string | null;
    installed_at: string;
    revoked_at: string | null;
}

export interface SlackChannel {
    id: string;
    name: string;
    is_private: boolean;
}

export function useSlackIntegration() {
    const api = useApi();
    return useQuery({
        queryKey: ["slack", "integration"],
        queryFn: () => api.get<SlackIntegration | null>("/integrations/slack"),
    });
}

export function useSlackInstallUrl() {
    const api = useApi();
    // Mutation rather than query so it's an explicit user action;
    // a query would refetch on every focus and the URL contains a nonce.
    return useMutation({
        mutationFn: () => api.get<{ url: string }>("/integrations/slack/install"),
    });
}

export function useSlackChannels(enabled: boolean) {
    const api = useApi();
    return useQuery({
        queryKey: ["slack", "channels"],
        queryFn: () => api.get<SlackChannel[]>("/integrations/slack/channels"),
        enabled,
    });
}

export function useSetSlackChannel() {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: ({ channel_id, channel_name }: { channel_id: string; channel_name?: string }) =>
            api.post<SlackIntegration>("/integrations/slack/channel", { channel_id, channel_name }),
        onSuccess: () => {
            qc.invalidateQueries({ queryKey: ["slack"] });
        },
    });
}

export function useUninstallSlack() {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: () => api.del<{ ok: boolean }>("/integrations/slack"),
        onSuccess: () => {
            qc.invalidateQueries({ queryKey: ["slack"] });
        },
    });
}
