"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useApi } from "./api";

// Types built from the actual /api/v1/emails*.py response shapes — not
// from any stale spec doc. If the backend evolves these, update here.

export type EmailStatus = "pending" | "sent" | "failed" | string;
export type EmailProvider = "google" | "microsoft" | string;

export interface EmailSendOut {
    id: string;
    interaction_id: string | null;
    provider: EmailProvider;
    to_address: string;
    cc_address: string | null;
    subject: string;
    status: EmailStatus;
    provider_message_id: string | null;
    error: string | null;
    sent_at: string | null;
    created_at: string;
}

export interface FollowUpDraftOut {
    interaction_id: string;
    suggested_to: string | null;
    draft_subject: string;
    draft_body: string;
    action_item_drafts: Array<{
        title: string;
        category: string | null;
        priority: string | null;
        draft: unknown;
    }>;
    recent_sends: EmailSendOut[];
}

export interface EmailSendListItem {
    id: string;
    interaction_id: string | null;
    interaction_title: string | null;
    interaction_channel: string | null;
    sender_user_id: string | null;
    sender_name: string | null;
    sender_email: string | null;
    provider: EmailProvider;
    to_address: string;
    cc_address: string | null;
    subject: string;
    body: string;
    status: EmailStatus;
    provider_message_id: string | null;
    error: string | null;
    sent_at: string | null;
    created_at: string;
}

export interface EmailSendListOut {
    items: EmailSendListItem[];
    total: number;
    limit: number;
    offset: number;
}

export interface CommunicationsListFilters {
    status?: "sent" | "failed" | "pending";
    dateFrom?: string;
    dateTo?: string;
    q?: string;
    limit?: number;
    offset?: number;
}

export interface EmailAttachmentInput {
    kind: "kb" | "upload";
    id: string;
    title?: string;
    mime_type?: string;
}

export interface SendFollowUpInput {
    subject: string;
    body: string;
    to: string;
    cc?: string;
    // Backend accepts only "google" | "microsoft" or omitted for auto.
    // The UI layer translates "auto" → undefined before mutating.
    provider?: "google" | "microsoft";
    attachments?: EmailAttachmentInput[];
}

function buildCommsQs(filters: CommunicationsListFilters): string {
    const params = new URLSearchParams();
    if (filters.status) params.set("status", filters.status);
    if (filters.dateFrom) params.set("date_from", filters.dateFrom);
    if (filters.dateTo) params.set("date_to", filters.dateTo);
    if (filters.q) params.set("q", filters.q);
    if (filters.limit !== undefined) params.set("limit", String(filters.limit));
    if (filters.offset !== undefined)
        params.set("offset", String(filters.offset));
    const qs = params.toString();
    return qs ? `?${qs}` : "";
}

export function useFollowUpDraft(interactionId: string | undefined) {
    const api = useApi();
    return useQuery({
        queryKey: ["follow-up-draft", interactionId],
        queryFn: () =>
            api.get<FollowUpDraftOut>(
                `/interactions/${interactionId}/follow-up-draft`,
            ),
        enabled: Boolean(interactionId),
        // Drafts are cheap to render but can change after the AI pipeline
        // re-runs — keep the cache fresh-ish without spamming the LLM.
        staleTime: 30_000,
        // Avoid auto-retry: a failed draft fetch usually means the
        // interaction isn't done processing yet, retrying won't help.
        retry: false,
    });
}

export function useSendFollowUp(interactionId: string | undefined) {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: (input: SendFollowUpInput) => {
            // Backend EmailSendIn currently accepts a single `to` and
            // single `cc`. The UI collects multi-recipients in an array
            // but joins on comma here so the user can still type
            // additional addresses; if/when backend accepts arrays we'll
            // shape the payload accordingly.
            const payload: Record<string, unknown> = {
                subject: input.subject,
                body: input.body,
                to: input.to,
            };
            if (input.cc) payload.cc = input.cc;
            if (input.provider) payload.provider = input.provider;
            if (input.attachments && input.attachments.length > 0)
                payload.attachments = input.attachments;
            return api.post<EmailSendOut>(
                `/interactions/${interactionId}/send-follow-up`,
                payload,
            );
        },
        onSuccess: () => {
            // Outbox listing + the draft (which embeds recent_sends) both
            // need to repopulate after a new send.
            qc.invalidateQueries({ queryKey: ["communications"] });
            qc.invalidateQueries({
                queryKey: ["follow-up-draft", interactionId],
            });
        },
    });
}

/**
 * Regenerate the follow-up email draft for an interaction.
 *
 * Hits the focused backend endpoint (one Sonnet call seeded with the
 * existing summary + action items), then invalidates the draft query
 * so the form re-hydrates with the new subject/body.
 */
export function useRegenerateFollowUpDraft(interactionId: string | undefined) {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: () =>
            api.post<FollowUpDraftOut>(
                `/interactions/${interactionId}/follow-up-draft/regenerate`,
                {},
            ),
        onSuccess: () => {
            qc.invalidateQueries({
                queryKey: ["follow-up-draft", interactionId],
            });
        },
    });
}

/**
 * Discard the saved follow-up draft for an interaction.
 *
 * Sends DELETE so the backend actually removes
 * insights.follow_up_email_draft. The UI was previously just hiding
 * the panel locally, which left the next visit re-rendering the same
 * stale draft.
 */
export function useDiscardFollowUpDraft(interactionId: string | undefined) {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: () =>
            api.del<void>(
                `/interactions/${interactionId}/follow-up-draft`,
            ),
        onSuccess: () => {
            qc.invalidateQueries({
                queryKey: ["follow-up-draft", interactionId],
            });
        },
    });
}

export function useCommunicationsList(
    filters: CommunicationsListFilters = {},
) {
    const api = useApi();
    return useQuery({
        queryKey: ["communications", filters],
        queryFn: () =>
            api.get<EmailSendListOut>(`/emails${buildCommsQs(filters)}`),
        // Keep the previous page visible while the next one loads —
        // avoids a full skeleton flash when the user types in the search
        // box or clicks "Next".
        placeholderData: (prev) => prev,
    });
}
