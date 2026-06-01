"use client";

import { useAuth } from "@clerk/nextjs";

const API_BASE = "/api/v1";

/**
 * Hook that returns a fetch wrapper pre-configured with the current
 * Clerk session token as an Authorization bearer.
 *
 * Send the raw JWT (no `clerk_` prefix). The earlier `clerk_` prefix
 * scheme confused Clerk's own Next.js middleware — it tried to
 * base64-decode the entire `clerk_<JWT>` string as a JWT and threw
 * `SyntaxError: Unexpected token 'r'... is not valid JSON` on every
 * authenticated request, which the SPA then rendered as a 500. The
 * backend's _principal_from_clerk verifies the JWT against Clerk's
 * JWKS directly — no marker prefix needed.
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
        if (token) headers.set("Authorization", `Bearer ${token}`);

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

    async function fetchRaw(
        path: string,
        init: RequestInit = {},
    ): Promise<Response> {
        // Escape hatch for multipart uploads (CSV import, file attachments
        // etc.) where we cannot let ``request`` set
        // ``Content-Type: application/json``. Authorization header is
        // still applied; the caller deals with the Response directly.
        const token = await getToken();
        const headers = new Headers(init.headers);
        headers.set("Accept", "application/json");
        if (token) headers.set("Authorization", `Bearer ${token}`);
        return fetch(`${API_BASE}${path}`, { ...init, headers });
    }

    return {
        request,
        fetchRaw,
        // Method-shaped sugar so call sites read like a normal REST
        // client. ``request`` stays exposed for the rare endpoint that
        // needs a custom init (form uploads, streaming, etc.).
        get: <T>(path: string, init: RequestInit = {}) =>
            request<T>(path, { ...init, method: "GET" }),
        post: <T>(path: string, body?: unknown, init: RequestInit = {}) =>
            request<T>(path, {
                ...init,
                method: "POST",
                body: body === undefined ? undefined : JSON.stringify(body),
            }),
        patch: <T>(path: string, body?: unknown, init: RequestInit = {}) =>
            request<T>(path, {
                ...init,
                method: "PATCH",
                body: body === undefined ? undefined : JSON.stringify(body),
            }),
        put: <T>(path: string, body?: unknown, init: RequestInit = {}) =>
            request<T>(path, {
                ...init,
                method: "PUT",
                body: body === undefined ? undefined : JSON.stringify(body),
            }),
        del: <T>(path: string, body?: unknown, init: RequestInit = {}) =>
            request<T>(path, {
                ...init,
                method: "DELETE",
                body: body === undefined ? undefined : JSON.stringify(body),
            }),
    };
}

export class ApiError extends Error {
    constructor(public status: number, message: string) {
        super(message);
    }
}
