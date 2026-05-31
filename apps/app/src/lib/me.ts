"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useApi } from "./api";

export type PlanTier = "sandbox" | "starter" | "growth" | "enterprise";
export type UserRole = "admin" | "manager" | "agent";

// Canonical motion vocabulary. Mirrors the backend CHECK constraints
// on ``tenants.default_domain`` / ``users.default_domain`` /
// ``action_plans.domain`` / ``interactions.domain`` and the
// ``CANONICAL_DOMAINS`` tuple in ``backend/app/auth.py``. The Manager
// portal renders one tab per domain the user has manager scope for;
// holding 2+ unlocks the cross-motion Journey view.
export type Domain = "sales" | "customer_service" | "it_support" | "generic";

export interface PlanLimits {
    real_time_transcription: boolean;
    live_coaching: boolean;
    crm_push: boolean;
    custom_scorecards: boolean;
    custom_branding: boolean;
    ask_linda: boolean;
    api_access: boolean;
    max_users: number | null;
    max_monthly_minutes: number | null;
    max_uploads_per_day: number | null;
    ai_model_tier: "haiku" | "sonnet" | "opus";
}

export interface Tenant {
    id: string;
    name: string;
    slug: string;
    plan_tier: PlanTier;
    is_white_label: boolean;
    trial_ends_at: string | null;
    trial_active: boolean;
    trial_expired: boolean;
    // True iff the tenant has an active Stripe subscription; drives
    // whether /billing shows the first-time plan picker or the
    // Stripe billing portal CTA.
    has_subscription: boolean;
    // True iff this tenant may render the role-preview pill. The
    // backend computes "sandbox OR override-on" and surfaces it as a
    // single boolean so the SPA doesn't have to keep the predicate
    // in sync.
    role_preview_enabled: boolean;
    limits: PlanLimits;
}

export interface MeUser {
    id: string;
    email: string;
    name: string | null;
    // The *effective* role: the preview-role overlay if it's currently
    // applied, otherwise the user's real role. Sidebar nav + role-gated
    // UI should read this directly — no double-processing.
    role: UserRole;
    // The user's stored override. NULL means "no preview, use real
    // role". Surfaced so the switcher can render which option is
    // checked even when the override isn't being applied.
    preview_role: UserRole | null;
    // The user's underlying ``users.role`` (no preview overlay). Lets
    // the SPA render "Switch back to <real>" when preview differs.
    real_role: UserRole;
    // True iff the principal resolver applied the preview overlay on
    // this request — drives the "preview mode" banner.
    is_previewing: boolean;
    // Motions the user works front-line in. Drives which agent
    // surfaces (inbox, action plans, coaching) the SPA shows. Empty
    // list means no agent surfaces — common for a dedicated Sales
    // Manager who only consumes dashboards. Added with backend
    // migration ``dom_001`` (2026-05-31).
    agent_domains: Domain[];
    // Motions the user has manager-level visibility into. Drives which
    // Manager sub-pages render; ``length >= 2`` unlocks the cross-
    // motion Journey view.
    manager_domains: Domain[];
    // Tenant Settings/Admin gate, orthogonal to manager scope. A
    // dedicated Sales Manager is ``manager_domains=["sales"]`` with
    // ``is_tenant_admin=false``; a founder is both.
    is_tenant_admin: boolean;
}

export interface Me {
    tenant: Tenant;
    user: MeUser | null;
}

export function useMe() {
    const api = useApi();
    return useQuery({
        queryKey: ["me"],
        queryFn: () => api.request<Me>("/me"),
    });
}

export interface SetPreviewRoleResponse {
    role: UserRole | null;
    real_role: UserRole;
}

/**
 * Tanstack mutation that sets or clears the calling user's preview
 * role. Sandbox-tier tenants only — the backend enforces the same
 * three-layer gate the principal resolver uses (tier + trial-active +
 * role validity), so a non-sandbox call returns 403. Invalidates the
 * ``["me"]`` query on success so the header pill, sidebar, and any
 * role-gated route immediately re-render against the new effective
 * role.
 */
export function useSetPreviewRole() {
    const api = useApi();
    const queryClient = useQueryClient();
    return useMutation({
        mutationFn: (body: { role: UserRole | null }) =>
            api.post<SetPreviewRoleResponse>("/me/preview-role", body),
        onSuccess: () => {
            queryClient.invalidateQueries({ queryKey: ["me"] });
        },
    });
}
