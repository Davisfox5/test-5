"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useApi } from "./api";

export type OAuthProvider =
    | "pipedrive"
    | "hubspot"
    | "salesforce"
    | "google"
    | "microsoft";

export interface Integration {
    id: string;
    provider: string;
    scopes: string[];
    expires_at: string | null;
    created_at: string;
}

export interface OAuthStatus {
    integrations: Integration[];
}

export function useOAuthStatus() {
    const api = useApi();
    return useQuery({
        queryKey: ["oauth-status"],
        queryFn: () => api.get<OAuthStatus>("/oauth/status"),
    });
}

export function useRevokeIntegration() {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: (provider: OAuthProvider) =>
            api.post<void>(`/oauth/${provider}/revoke`),
        onSuccess: () => qc.invalidateQueries({ queryKey: ["oauth-status"] }),
    });
}

// Mint a one-shot authorize URL via the authenticated POST /ticket
// endpoint. We can't link the user straight to GET /authorize because
// anchor clicks don't carry the Bearer JWT — so we fetch the authorize
// URL ourselves and then `window.location =` to the provider.
export function useStartOAuth() {
    const api = useApi();
    return useMutation({
        mutationFn: async (provider: OAuthProvider) => {
            const { authorize_url } = await api.post<{ authorize_url: string }>(
                `/oauth/${provider}/ticket`,
            );
            return authorize_url;
        },
    });
}
