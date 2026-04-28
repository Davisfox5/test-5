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

// Authorize is a redirect endpoint, so the SPA opens it in a new tab
// rather than fetching it. This helper returns the URL with the same
// /api/v1 prefix the rest of the SPA proxies through.
export function authorizeUrlFor(provider: OAuthProvider): string {
    return `/api/v1/oauth/${provider}/authorize`;
}
