"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, useApi } from "./api";

// Types built from the actual /api/v1/outreach.py + /api/v1/prospects
// response shapes (see backend/app/api/outreach.py and
// backend/app/services/outreach/common.py) — not from any stale spec
// doc. If the backend evolves these, update here.

// ── Campaign config (validated server-side against OutreachConfig) ─────

export type EmailFontFamily =
    | "arial"
    | "helvetica"
    | "georgia"
    | "times"
    | "verdana"
    | "tahoma"
    | "trebuchet"
    | "courier";

export interface OutreachTemplateConfig {
    subject: string;
    body: string;
    sender_name: string;
    sender_business: string;
    physical_address: string;
    font_family?: EmailFontFamily | null;
    font_size_px?: number | null;
    include_logo: boolean;
}

export interface OutreachAttachmentRef {
    s3_key: string;
    filename: string;
    content_type?: string | null;
    size_bytes?: number | null;
}

export interface SendWindowConfig {
    start_hour: number;
    end_hour: number;
    timezone: string;
    // ISO weekday numbers, 1=Mon … 7=Sun.
    days: number[];
}

export interface OutreachStepConfig {
    offset_days: number;
    guidance?: string | null;
}

export interface OutreachConfigShape {
    template: OutreachTemplateConfig;
    send_window: SendWindowConfig;
    steps: OutreachStepConfig[];
    daily_limit?: number | null;
    max_touches: number;
    mode: "review" | "auto";
    provider?: "google" | "microsoft" | null;
    attachments: OutreachAttachmentRef[];
}

// ── Campaigns ────────────────────────────────────────────────────────

export type OutreachCampaignStatus =
    | "draft"
    | "active"
    | "paused"
    | "completed"
    | string;

export interface MemberSkipOut {
    prospect_id: string;
    reason: string;
}

// Mirrors _quota_state's return dict exactly (see backend/app/api/
// outreach.py) — daily_limit/sent_today are campaign-scoped,
// tenant_daily_cap/tenant_sent_today are tenant-wide.
export interface OutreachQuota {
    daily_limit: number;
    sent_today: number;
    remaining_today: number;
    tenant_daily_cap: number;
    tenant_sent_today: number;
}

export interface OutreachCampaignOut {
    id: string;
    name: string;
    kind: string;
    status: OutreachCampaignStatus;
    config: OutreachConfigShape;
    sent_count: number;
    started_at: string | null;
    ended_at: string | null;
    created_at: string;
    member_states: Record<string, number>;
    quota: OutreachQuota | null;
    skipped: MemberSkipOut[];
}

// State machine driven by services/outreach/scheduler.py:
// draft_pending -> needs_approval -> queued -> in_sequence -> completed
// (halted / failed can interrupt at any point). draft_status is one of
// null (no draft yet), "ready" (awaiting approval), "approved".
export interface OutreachMemberOut {
    id: string;
    campaign_id: string;
    prospect_id: string;
    prospect_name: string | null;
    contact_email: string | null;
    state: string;
    current_step: number;
    touches_sent: number;
    next_send_at: string | null;
    last_sent_at: string | null;
    replied_at: string | null;
    halt_reason: string | null;
    draft_subject: string | null;
    draft_body: string | null;
    draft_status: string | null;
    personalization: Record<string, unknown>;
}

export interface MemberListOut {
    items: OutreachMemberOut[];
    total: number;
    limit: number;
    offset: number;
}

export interface CreateOutreachCampaignInput {
    name: string;
    config: OutreachConfigShape;
    prospect_ids: string[];
}

export interface MemberPatchInput {
    draft_subject?: string;
    draft_body?: string;
    action?: "approve" | "reject";
}

export interface ApproveDraftsInput {
    member_ids?: string[] | null;
    all?: boolean;
}

// ── Prospects ────────────────────────────────────────────────────────

export interface ProspectMembershipOut {
    campaign_id: string;
    campaign_name: string;
    member_id: string;
    state: string;
    touches_sent: number;
    next_send_at: string | null;
    last_sent_at: string | null;
}

export interface ProspectOut {
    prospect_id: string;
    business_name: string;
    domain: string | null;
    pipeline_status: string | null;
    pipeline_status_changed_at: string | null;
    do_not_contact: boolean;
    city?: string | null;
    state?: string | null;
    segment?: string | null;
    current_software?: string | null;
    hook?: string | null;
    source?: string | null;
    instagram?: string | null;
    primary_contact: {
        id: string;
        name: string | null;
        email: string | null;
        phone: string | null;
    } | null;
    memberships: ProspectMembershipOut[];
    last_interaction_at: string | null;
}

export interface ProspectListOut {
    items: ProspectOut[];
    total: number;
    limit: number;
    offset: number;
}

export interface ProspectFilters {
    limit?: number;
    offset?: number;
    status?: string;
    q?: string;
}

// ── Uploads: email logo + campaign attachments ──────────────────────

export interface EmailLogoOut {
    filename: string;
    content_type: string;
    size_bytes: number | null;
    url: string | null;
}

export interface OutreachUploadOut {
    s3_key: string;
    filename: string;
    content_type: string | null;
    size_bytes: number;
}

// ── Error formatting ─────────────────────────────────────────────────

/**
 * Turn a raw backend `detail` payload into a readable string. Plain
 * strings pass through untouched; a pydantic 422 detail (list of
 * {loc, msg, type}) is rendered as "field.path: message; ..." instead
 * of the useless "[object Object]" you'd get from naive stringification.
 */
export function formatApiDetail(detail: unknown): string {
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
        return detail
            .map((entry) => {
                if (entry && typeof entry === "object") {
                    const e = entry as { loc?: unknown; msg?: unknown };
                    const loc = Array.isArray(e.loc) ? e.loc.join(".") : "";
                    const msg = e.msg != null ? String(e.msg) : JSON.stringify(entry);
                    return loc ? `${loc}: ${msg}` : msg;
                }
                return String(entry);
            })
            .join("; ");
    }
    if (detail == null) return "Request failed";
    return JSON.stringify(detail);
}

async function postMultipart<T>(
    api: ReturnType<typeof useApi>,
    path: string,
    file: File,
): Promise<T> {
    const fd = new FormData();
    fd.append("file", file);
    const resp = await api.fetchRaw(path, { method: "POST", body: fd });
    if (!resp.ok) {
        let detail = `HTTP ${resp.status}`;
        try {
            const body = await resp.json();
            if (body?.detail) detail = formatApiDetail(body.detail);
        } catch {
            // no JSON body — keep the generic HTTP status message
        }
        throw new ApiError(resp.status, detail);
    }
    if (resp.status === 204) return undefined as T;
    return (await resp.json()) as T;
}

function buildQs(params: object): string {
    const qs = new URLSearchParams();
    for (const [key, val] of Object.entries(
        params as Record<string, string | number | undefined>,
    )) {
        if (val !== undefined && val !== "") qs.set(key, String(val));
    }
    const s = qs.toString();
    return s ? `?${s}` : "";
}

// ── Campaign hooks ───────────────────────────────────────────────────

export function useOutreachCampaigns(status?: string) {
    const api = useApi();
    return useQuery({
        queryKey: ["outreach-campaigns", status],
        queryFn: () =>
            api.get<OutreachCampaignOut[]>(
                `/outreach/campaigns${buildQs({ status })}`,
            ),
    });
}

export function useOutreachCampaign(id: string | undefined) {
    const api = useApi();
    return useQuery({
        queryKey: ["outreach-campaign", id],
        queryFn: () => api.get<OutreachCampaignOut>(`/outreach/campaigns/${id}`),
        enabled: Boolean(id),
    });
}

export function useCreateOutreachCampaign() {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: async (input: CreateOutreachCampaignInput) => {
            const resp = await api.fetchRaw("/outreach/campaigns", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(input),
            });
            if (!resp.ok) {
                let detail = `HTTP ${resp.status}`;
                try {
                    const body = await resp.json();
                    if (body?.detail) detail = formatApiDetail(body.detail);
                } catch {
                    // no JSON body — keep the generic HTTP status message
                }
                throw new ApiError(resp.status, detail);
            }
            return (await resp.json()) as OutreachCampaignOut;
        },
        onSuccess: () => {
            qc.invalidateQueries({ queryKey: ["outreach-campaigns"] });
        },
    });
}

export function useCampaignMembers(
    campaignId: string | undefined,
    { limit = 50, offset = 0 }: { limit?: number; offset?: number } = {},
) {
    const api = useApi();
    return useQuery({
        queryKey: ["outreach-members", campaignId, limit, offset],
        queryFn: () =>
            api.get<MemberListOut>(
                `/outreach/campaigns/${campaignId}/members${buildQs({ limit, offset })}`,
            ),
        enabled: Boolean(campaignId),
        placeholderData: (prev) => prev,
    });
}

export function useGenerateDrafts(campaignId: string | undefined) {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: (memberIds?: string[] | null) =>
            api.post(`/outreach/campaigns/${campaignId}/generate-drafts`, {
                member_ids: memberIds ?? null,
            }),
        onSuccess: () => {
            qc.invalidateQueries({ queryKey: ["outreach-members", campaignId] });
            qc.invalidateQueries({ queryKey: ["outreach-campaign", campaignId] });
        },
    });
}

export function useApproveDrafts(campaignId: string | undefined) {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: (input: ApproveDraftsInput) =>
            api.post(`/outreach/campaigns/${campaignId}/approve-drafts`, input),
        onSuccess: () => {
            qc.invalidateQueries({ queryKey: ["outreach-members", campaignId] });
            qc.invalidateQueries({ queryKey: ["outreach-campaign", campaignId] });
        },
    });
}

export function usePatchMember(campaignId: string | undefined) {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: ({
            memberId,
            patch,
        }: {
            memberId: string;
            patch: MemberPatchInput;
        }) => api.patch<OutreachMemberOut>(`/outreach/members/${memberId}`, patch),
        onSuccess: () => {
            qc.invalidateQueries({ queryKey: ["outreach-members", campaignId] });
            qc.invalidateQueries({ queryKey: ["outreach-campaign", campaignId] });
        },
    });
}

export function useActivateCampaign(campaignId: string | undefined) {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: () =>
            api.post<OutreachCampaignOut>(
                `/outreach/campaigns/${campaignId}/activate`,
            ),
        onSuccess: () => {
            qc.invalidateQueries({ queryKey: ["outreach-campaign", campaignId] });
            qc.invalidateQueries({ queryKey: ["outreach-campaigns"] });
        },
    });
}

export function usePauseCampaign(campaignId: string | undefined) {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: () =>
            api.post<OutreachCampaignOut>(
                `/outreach/campaigns/${campaignId}/pause`,
            ),
        onSuccess: () => {
            qc.invalidateQueries({ queryKey: ["outreach-campaign", campaignId] });
            qc.invalidateQueries({ queryKey: ["outreach-campaigns"] });
        },
    });
}

// ── Prospects ────────────────────────────────────────────────────────

export function useProspects(filters: ProspectFilters = {}) {
    const api = useApi();
    return useQuery({
        queryKey: ["prospects", filters],
        queryFn: () =>
            api.get<ProspectListOut>(`/prospects${buildQs(filters)}`),
        placeholderData: (prev) => prev,
    });
}

// ── Email branding (logo) ───────────────────────────────────────────

export function useEmailLogo() {
    const api = useApi();
    return useQuery({
        queryKey: ["email-logo"],
        queryFn: async () => {
            try {
                return await api.get<EmailLogoOut>("/outreach/email-logo");
            } catch (err) {
                if (err instanceof ApiError && err.status === 404) return null;
                throw err;
            }
        },
        // A 404 here just means "no logo uploaded yet" — retrying won't
        // change that, so don't spam the endpoint.
        retry: false,
    });
}

export function useUploadEmailLogo() {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: (file: File) =>
            postMultipart<EmailLogoOut>(api, "/outreach/email-logo", file),
        onSuccess: () => {
            qc.invalidateQueries({ queryKey: ["email-logo"] });
        },
    });
}

export function useDeleteEmailLogo() {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: () => api.del<void>("/outreach/email-logo"),
        onSuccess: () => {
            qc.invalidateQueries({ queryKey: ["email-logo"] });
        },
    });
}

export function useUploadOutreachAttachment() {
    const api = useApi();
    return useMutation({
        mutationFn: (file: File) =>
            postMultipart<OutreachUploadOut>(api, "/outreach/uploads", file),
    });
}
