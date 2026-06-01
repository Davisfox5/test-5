"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { Domain } from "./me";
import { useApi } from "./api";

export interface MotionRule {
    id: string;
    group_name: string;
    agent_domains: Domain[];
    manager_domains: Domain[];
    grants_tenant_admin: boolean;
    is_active: boolean;
    description: string | null;
    created_at: string;
    updated_at: string;
}

export interface MotionRuleCreatePayload {
    group_name: string;
    agent_domains?: Domain[];
    manager_domains?: Domain[];
    grants_tenant_admin?: boolean;
    is_active?: boolean;
    description?: string;
}

export type MotionRulePatchPayload = Partial<MotionRuleCreatePayload>;

export interface TestResolveResult {
    matched_rule_count: number;
    agent_domains: Domain[];
    manager_domains: Domain[];
    is_tenant_admin: boolean;
}

export function useMotionRules() {
    const api = useApi();
    return useQuery({
        queryKey: ["sso", "motion-rules"],
        queryFn: () => api.get<MotionRule[]>("/admin/motion-provisioning-rules"),
    });
}

export function useCreateMotionRule() {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: (body: MotionRuleCreatePayload) =>
            api.post<MotionRule>("/admin/motion-provisioning-rules", body),
        onSuccess: () => {
            qc.invalidateQueries({ queryKey: ["sso", "motion-rules"] });
        },
    });
}

export function usePatchMotionRule() {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: ({
            id,
            patch,
        }: {
            id: string;
            patch: MotionRulePatchPayload;
        }) =>
            api.patch<MotionRule>(
                `/admin/motion-provisioning-rules/${id}`,
                patch,
            ),
        onSuccess: () => {
            qc.invalidateQueries({ queryKey: ["sso", "motion-rules"] });
        },
    });
}

export function useDeleteMotionRule() {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: (id: string) =>
            api.del<void>(`/admin/motion-provisioning-rules/${id}`),
        onSuccess: () => {
            qc.invalidateQueries({ queryKey: ["sso", "motion-rules"] });
        },
    });
}

export function useTestResolveScopes() {
    const api = useApi();
    return useMutation({
        mutationFn: (group_names: string[]) =>
            api.post<TestResolveResult>("/admin/sso/test-resolve", {
                group_names,
            }),
    });
}
