"use client";

import { useQuery } from "@tanstack/react-query";
import { useApi } from "./api";

export type PlanTier = "sandbox" | "starter" | "growth" | "enterprise";
export type UserRole = "admin" | "manager" | "agent";

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
    limits: PlanLimits;
}

export interface MeUser {
    id: string;
    email: string;
    name: string | null;
    role: UserRole;
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
