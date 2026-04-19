"use client";

import { useAuth } from "@clerk/nextjs";

const API_BASE = "/api/v1";

/**
 * Hook that returns a fetch wrapper pre-configured with the current
 * Clerk session token as an Authorization bearer. The FastAPI backend
 * recognises `Bearer clerk_<user_id>` and resolves the tenant from the
 * user's membership.
 */
export function useApi() {
    const { getToken } = useAuth();

    async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
        const token = await getToken();
        const headers = new Headers(init.headers);
        headers.set("Accept", "application/json");
        if (init.body && !headers.has("Content-Type")) {
            headers.set("Content-Type", "application/json");
        }
        if (token) headers.set("Authorization", `Bearer clerk_${token}`);

        const resp = await fetch(`${API_BASE}${path}`, { ...init, headers });
        if (!resp.ok) {
            let detail = `HTTP ${resp.status}`;
            try {
                const body = await resp.json();
                if (body?.detail) detail = body.detail;
            } catch {}
            throw new ApiError(resp.status, detail);
        }
        if (resp.status === 204) return undefined as T;
        return (await resp.json()) as T;
    }

    return { request };
}

export class ApiError extends Error {
    constructor(public status: number, message: string) {
        super(message);
    }
}
