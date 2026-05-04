"use client";

import { useQuery } from "@tanstack/react-query";
import { useApi } from "./api";

export type CustomerOwnerOut = {
    user_id: string;
    name: string | null;
    email: string | null;
    role: "primary" | "secondary";
    assigned_via: "first_uploader" | "speaker_tag" | "manual";
};

export type CustomerListItem = {
    id: string;
    name: string;
    domain: string | null;
    industry: string | null;
    parent_customer_id: string | null;
    timezone: string | null;
    owners: CustomerOwnerOut[];
    contact_count: number;
    multithreading_90d: number;
    latest_interaction_at: string | null;
    latest_interaction_id: string | null;
    latest_interaction_title: string | null;
    sentiment_score: number | null;
    churn_risk: number | null;
    open_action_items: number;
};

export type CustomerListResponse = {
    items: CustomerListItem[];
    total: number;
};

export type CustomerListSort =
    | "latest_interaction"
    | "name"
    | "churn_risk"
    | "open_action_items"
    | "multithreading_90d";

export function useCustomerList(params?: {
    name?: string;
    ownerUserId?: string;
    sort?: CustomerListSort;
    limit?: number;
    offset?: number;
}) {
    const api = useApi();
    const limit = params?.limit ?? 50;
    const offset = params?.offset ?? 0;
    const sort = params?.sort ?? "latest_interaction";
    const search = new URLSearchParams();
    search.set("limit", String(limit));
    search.set("offset", String(offset));
    search.set("sort", sort);
    if (params?.name) search.set("name", params.name);
    if (params?.ownerUserId) search.set("owner_user_id", params.ownerUserId);

    return useQuery({
        queryKey: [
            "customers-list",
            { sort, name: params?.name ?? "", ownerUserId: params?.ownerUserId ?? "", limit, offset },
        ],
        queryFn: () =>
            api.get<CustomerListResponse>(`/customers/list?${search.toString()}`),
    });
}

/** Auto-favicon URL via Google's favicon service.
 *
 * Cheap, anonymous, returns a 1×1 transparent PNG when the domain has no
 * favicon — so the <img> just stays blank rather than throwing. Using
 * Google here (not Clearbit) avoids the per-request Clearbit token; we
 * can swap later if image quality matters more than cost.
 */
export function faviconFor(domain: string | null | undefined): string | null {
    if (!domain) return null;
    return `https://www.google.com/s2/favicons?domain=${encodeURIComponent(domain)}&sz=64`;
}
