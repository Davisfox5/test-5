"use client";

import { useAuth } from "@clerk/nextjs";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useApi } from "./api";

/* ── Types ──────────────────────────────────────────────────────────── */

// `source_type` is open-ended on the backend ("editor", "upload",
// "confluence", "notion", "gdrive", or any third-party identifier),
// so keep it a string and just enumerate the common values for the
// filter dropdown.
export type KBSourceType =
    | "editor"
    | "upload"
    | "confluence"
    | "notion"
    | "gdrive"
    | "onedrive"
    | "sharepoint"
    | string;

export interface KBDoc {
    id: string;
    tenant_id: string;
    title: string | null;
    content: string | null;
    source_type: KBSourceType | null;
    source_url: string | null;
    tags: string[];
    last_synced_at: string | null;
    created_at: string;
}

export interface KBDocCreatePayload {
    title: string;
    content: string;
    tags?: string[];
    source_type?: string;
}

export interface KBDocUpdatePayload {
    title?: string;
    content?: string;
    tags?: string[];
}

export interface KBListParams {
    limit?: number;
    offset?: number;
    q?: string;
    tags?: string;
    source_type?: string;
}

/* ── Queries ────────────────────────────────────────────────────────── */

function buildListQuery(params: KBListParams): string {
    const sp = new URLSearchParams();
    if (params.limit !== undefined) sp.set("limit", String(params.limit));
    if (params.offset !== undefined) sp.set("offset", String(params.offset));
    if (params.tags) sp.set("tags", params.tags);
    if (params.source_type) sp.set("source_type", params.source_type);
    const qs = sp.toString();
    return qs ? `?${qs}` : "";
}

// The backend's list endpoint doesn't accept a `q` parameter — it only
// filters by tags + source_type. We do the title/content text match
// client-side after the list resolves, which is fine for the small KB
// sizes we expect; if a tenant grows to thousands of docs we can
// switch this to call /kb/search and merge.
function applyClientSearch(rows: KBDoc[], q: string | undefined): KBDoc[] {
    if (!q) return rows;
    const needle = q.trim().toLowerCase();
    if (!needle) return rows;
    return rows.filter((d) => {
        const title = (d.title ?? "").toLowerCase();
        const content = (d.content ?? "").toLowerCase();
        return title.includes(needle) || content.includes(needle);
    });
}

export function useKBDocs(params: KBListParams = {}) {
    const api = useApi();
    const { q, ...listParams } = params;
    return useQuery({
        queryKey: ["kb-docs", listParams, q ?? null],
        queryFn: async () => {
            const rows = await api.get<KBDoc[]>(
                `/kb/docs${buildListQuery(listParams)}`,
            );
            return applyClientSearch(rows, q);
        },
        staleTime: 15_000,
    });
}

export function useKBDoc(id: string | undefined) {
    const api = useApi();
    return useQuery({
        queryKey: ["kb-doc", id],
        queryFn: () => api.get<KBDoc>(`/kb/docs/${id}`),
        enabled: Boolean(id),
    });
}

/* ── Mutations ──────────────────────────────────────────────────────── */

export function useCreateKBDoc() {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: (payload: KBDocCreatePayload) =>
            api.post<KBDoc>("/kb/docs", payload),
        onSuccess: () => {
            qc.invalidateQueries({ queryKey: ["kb-docs"] });
        },
    });
}

// Backend exposes update at PUT (not PATCH) — see knowledge_base.py.
export function useUpdateKBDoc() {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: ({ id, patch }: { id: string; patch: KBDocUpdatePayload }) =>
            api.put<KBDoc>(`/kb/docs/${id}`, patch),
        onSuccess: (data) => {
            qc.invalidateQueries({ queryKey: ["kb-docs"] });
            qc.setQueryData(["kb-doc", data.id], data);
        },
    });
}

export function useDeleteKBDoc() {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: (id: string) => api.del<void>(`/kb/docs/${id}`),
        onSuccess: (_data, id) => {
            qc.invalidateQueries({ queryKey: ["kb-docs"] });
            qc.invalidateQueries({ queryKey: ["kb-doc", id] });
        },
    });
}

/**
 * Multipart upload to /kb/upload — the backend extracts text from the
 * uploaded file (txt/pdf/docx) and creates a fresh KBDocument that is
 * returned to the caller. Native `fetch` is fine here because we don't
 * need progress events for the small files customers paste in.
 */
export function useUploadKBFile() {
    const { getToken } = useAuth();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: async (file: File): Promise<KBDoc> => {
            const token = await getToken();
            const fd = new FormData();
            fd.append("file", file);
            const headers = new Headers();
            headers.set("Accept", "application/json");
            if (token) headers.set("Authorization", `Bearer ${token}`);
            const resp = await fetch("/api/v1/kb/upload", {
                method: "POST",
                headers,
                body: fd,
            });
            if (!resp.ok) {
                let detail = `HTTP ${resp.status}`;
                try {
                    const body = await resp.json();
                    if (body?.detail) detail = body.detail;
                } catch {
                    // fall through with default detail
                }
                throw new Error(detail);
            }
            return (await resp.json()) as KBDoc;
        },
        onSuccess: () => {
            qc.invalidateQueries({ queryKey: ["kb-docs"] });
        },
    });
}
