"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useApi } from "./api";

// ──────────────────────────────────────────────────────────
// Shapes — mirror backend/app/api/action_plans.py response shapes.
// Kept wide on optional fields so older fixtures don't break the SPA.
// ──────────────────────────────────────────────────────────

export type ActionStepState =
    | "blocked"
    | "ready"
    | "in_progress"
    | "awaiting_response"
    | "done"
    | "skipped"
    | "deleted";

export type ActionStepRole =
    | "preparation"
    | "customer_endpoint"
    | "post_completion";

export type ComplianceLevel = "must" | "should" | "may";

export type ArtifactKind =
    | "email"
    | "script"
    | "research"
    | "meeting"
    | "system_write_payload"
    | "note";

export interface StepArtifact {
    id: string;
    version: number;
    kind: ArtifactKind | string;
    payload: Record<string, unknown>;
    model_tier: string | null;
    generated_at: string;
    superseded_at: string | null;
}

export interface StepResponse {
    id: string;
    source: "inbound_email" | "manual_note" | "auto_mark_done" | "outbound_email_sent" | string;
    note_text: string | null;
    extracted_data: Record<string, unknown>;
    unfilled_reasons: Record<string, string>;
    extraction_confidence: number | null;
    source_quotes: Record<string, string>;
    received_at: string;
    agent_overridden: boolean;
}

export interface ActionStepSlot {
    slot_key: string;
    description: string;
    required: boolean;
    filled_by_step_id: string | null;
    filled_value: unknown | null;
    filled_at: string | null;
}

export interface ActionStepOutputSlot {
    slot_key: string;
    description: string;
    type?: string;
}

export interface ActionStepParticipant {
    name?: string;
    role?: string;
    side?: "customer" | "vendor" | string;
    source?: string;
    email?: string | null;
}

export interface ActionStep {
    id: string;
    plan_id: string;
    assigned_to: string | null;
    title: string;
    description: string | null;
    intent: string | null;
    priority: string;
    due_date: string | null;
    recommended_channel: string | null;
    channel_reasoning: string | null;
    participants: ActionStepParticipant[];
    prep_artifacts: string[];
    implicit_signal: string | null;
    state: ActionStepState;
    started_at: string | null;
    completed_at: string | null;
    skipped_at: string | null;
    deleted_at: string | null;
    depends_on: string[];
    input_slots: ActionStepSlot[];
    output_schema: ActionStepOutputSlot[];
    output_data: Record<string, unknown>;
    kb_source: Record<string, unknown> | null;
    compliance_level: ComplianceLevel | null;
    role_in_plan: ActionStepRole;
    target_integration: string | null;
    integration_operation: string | null;
    artifact_version: number;
    artifact_stale: boolean;
    regen_debounce_until: string | null;
    skip_reason: string | null;
    /** True when the step's outbound action expects a customer reply.
     * Send button uses this to decide whether to transition to
     * awaiting_response (needs reply) or done (fire-and-forget). */
    awaits_response: boolean;
    created_at: string;
    latest_artifact: StepArtifact | null;
    responses: StepResponse[];
}

export interface ActionPlan {
    id: string;
    tenant_id: string;
    interaction_id: string | null;
    customer_id: string | null;
    goal: string | null;
    domain: "sales" | "customer_service" | "it_support" | "generic" | string;
    status: "draft" | "active" | "completed" | "abandoned" | string;
    customer_endpoint_step_id: string | null;
    procedures_applied: Array<Record<string, unknown>>;
    external_context_snapshot: Record<string, unknown>;
    version: number;
    manually_created: boolean;
    created_at: string;
    completed_at: string | null;
    steps: ActionStep[];
}

// ──────────────────────────────────────────────────────────
// Hooks
// ──────────────────────────────────────────────────────────

export function useActionPlans(opts?: {
    status?: string;
    interactionId?: string;
    customerId?: string;
}) {
    const api = useApi();
    const params = new URLSearchParams();
    if (opts?.status) params.set("status", opts.status);
    if (opts?.interactionId) params.set("interaction_id", opts.interactionId);
    if (opts?.customerId) params.set("customer_id", opts.customerId);
    const qs = params.toString();
    return useQuery({
        queryKey: ["action-plans", opts?.status ?? "all", opts?.interactionId ?? "", opts?.customerId ?? ""],
        queryFn: async () => {
            return api.request<{ items: ActionPlan[] }>(
                `/action-plans${qs ? `?${qs}` : ""}`,
            );
        },
    });
}

export function useActionPlan(planId: string | null | undefined) {
    const api = useApi();
    return useQuery({
        queryKey: ["action-plan", planId],
        queryFn: async () => {
            if (!planId) return null;
            return api.request<ActionPlan>(`/action-plans/${planId}`);
        },
        enabled: !!planId,
    });
}

function invalidatePlan(qc: ReturnType<typeof useQueryClient>, planId: string) {
    qc.invalidateQueries({ queryKey: ["action-plan", planId] });
    qc.invalidateQueries({ queryKey: ["action-plans"] });
}

export function useCompleteStep(planId: string) {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: async (vars: { stepId: string; output_data?: Record<string, unknown> }) =>
            api.request<ActionPlan>(
                `/action-plans/${planId}/steps/${vars.stepId}/complete`,
                {
                    method: "POST",
                    body: JSON.stringify({ output_data: vars.output_data ?? null }),
                },
            ),
        onSuccess: () => invalidatePlan(qc, planId),
    });
}

export function useSkipStep(planId: string) {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: async (vars: { stepId: string; reason?: string }) =>
            api.request<ActionPlan>(
                `/action-plans/${planId}/steps/${vars.stepId}/skip`,
                {
                    method: "POST",
                    body: JSON.stringify({ reason: vars.reason ?? null }),
                },
            ),
        onSuccess: () => invalidatePlan(qc, planId),
    });
}

export function useRestoreStep(planId: string) {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: async (vars: { stepId: string }) =>
            api.request<ActionPlan>(
                `/action-plans/${planId}/steps/${vars.stepId}/restore`,
                { method: "POST" },
            ),
        onSuccess: () => invalidatePlan(qc, planId),
    });
}

export interface StepEditPayload {
    title?: string;
    description?: string;
    intent?: string;
    priority?: "high" | "medium" | "low";
    due_date?: string;  // YYYY-MM-DD or "" to clear
    recommended_channel?: string;
    channel_reasoning?: string;
    awaits_response?: boolean;
}

export function useEditStep(planId: string) {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: async (vars: { stepId: string; patch: StepEditPayload }) =>
            api.request<ActionPlan>(
                `/action-plans/${planId}/steps/${vars.stepId}`,
                {
                    method: "PATCH",
                    body: JSON.stringify(vars.patch),
                },
            ),
        onSuccess: () => invalidatePlan(qc, planId),
    });
}

export function useDeleteStep(planId: string) {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: async (vars: { stepId: string }) =>
            api.request<ActionPlan>(
                `/action-plans/${planId}/steps/${vars.stepId}`,
                { method: "DELETE" },
            ),
        onSuccess: () => invalidatePlan(qc, planId),
    });
}

export function useAddNote(planId: string) {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: async (vars: { stepId: string; note_text: string }) =>
            api.request<ActionPlan>(
                `/action-plans/${planId}/steps/${vars.stepId}/notes`,
                {
                    method: "POST",
                    body: JSON.stringify({ note_text: vars.note_text }),
                },
            ),
        onSuccess: () => invalidatePlan(qc, planId),
    });
}

export function useOverrideSlot(planId: string) {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: async (vars: { stepId: string; slot_key: string; value: unknown }) =>
            api.request<ActionPlan>(
                `/action-plans/${planId}/steps/${vars.stepId}/override`,
                {
                    method: "POST",
                    body: JSON.stringify({
                        slot_key: vars.slot_key,
                        value: vars.value,
                    }),
                },
            ),
        onSuccess: () => invalidatePlan(qc, planId),
    });
}

export function useRecordSent(planId: string) {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: async (vars: { stepId: string; outbound_message_id: string }) =>
            api.request<ActionPlan>(
                `/action-plans/${planId}/steps/${vars.stepId}/sent`,
                {
                    method: "POST",
                    body: JSON.stringify({
                        outbound_message_id: vars.outbound_message_id,
                    }),
                },
            ),
        onSuccess: () => invalidatePlan(qc, planId),
    });
}
