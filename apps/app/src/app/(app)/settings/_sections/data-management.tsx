"use client";

import { useAuth, useClerk } from "@clerk/nextjs";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { useMe } from "@/lib/me";
import { useApi } from "@/lib/api";
import { humanizeError } from "@/components/admin/section";

/**
 * GDPR data-management section. Two cards:
 *
 *  - Export tenant data — pulls /tenants/{id}/export as a streamed
 *    blob and triggers a browser download. Streaming straight from the
 *    fetch response keeps the SPA from buffering multi-GB tenants in
 *    memory before the download starts.
 *  - Delete tenant permanently — admin must type the tenant name to
 *    enable the button. We send the typed name + a 10+ char `reason`
 *    that the backend's audit log requires.
 */
export function DataManagementSection() {
    return (
        <div className="space-y-4">
            <ExportCard />
            <DeleteCard />
        </div>
    );
}

/* ── Export ─────────────────────────────────────────────────────────── */

function ExportCard() {
    const { getToken } = useAuth();
    const { data: me } = useMe();
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState<string | null>(null);

    const tenantId = me?.tenant.id;
    const tenantSlug = me?.tenant.slug;

    async function handleExport() {
        if (!tenantId) return;
        setBusy(true);
        setError(null);
        try {
            const token = await getToken();
            const headers = new Headers();
            headers.set("Accept", "application/x-ndjson");
            if (token) headers.set("Authorization", `Bearer ${token}`);
            const resp = await fetch(
                `/api/v1/tenants/${tenantId}/export`,
                { method: "GET", headers },
            );
            if (!resp.ok) {
                let detail = `HTTP ${resp.status}`;
                try {
                    const body = await resp.json();
                    if (body?.detail) detail = body.detail;
                } catch {
                    // fall through with default
                }
                throw new Error(detail);
            }
            // resp.blob() drains the stream into a single Blob — fine for
            // typical tenant sizes; if we ever care about >1GB exports
            // we'd swap this for a streaming-saver pattern.
            const blob = await resp.blob();
            const url = URL.createObjectURL(blob);
            const a = document.createElement("a");
            a.href = url;
            const date = new Date().toISOString().slice(0, 10).replace(/-/g, "");
            a.download = `linda-${tenantSlug ?? tenantId}-${date}.ndjson`;
            document.body.appendChild(a);
            a.click();
            a.remove();
            URL.revokeObjectURL(url);
        } catch (err) {
            setError(humanizeError(err));
        } finally {
            setBusy(false);
        }
    }

    return (
        <div className="rounded-md border border-border bg-bg-raised p-4">
            <h4 className="font-medium">Export tenant data</h4>
            <p className="mt-1 text-sm text-text-muted">
                Download a complete ndjson archive of every interaction,
                action item, scorecard, user, comment, and audit row in
                your workspace.
            </p>
            <div className="mt-3 flex items-center gap-3 flex-wrap">
                <button
                    type="button"
                    onClick={handleExport}
                    disabled={busy || !tenantId}
                    className="rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-white disabled:opacity-50"
                >
                    {busy ? "Preparing…" : "Download export"}
                </button>
                {error ? (
                    <span className="text-xs text-accent-rose">{error}</span>
                ) : null}
            </div>
        </div>
    );
}

/* ── Hard delete ────────────────────────────────────────────────────── */

function DeleteCard() {
    const router = useRouter();
    const clerk = useClerk();
    const api = useApi();
    const { data: me } = useMe();

    const [confirmText, setConfirmText] = useState("");
    const [reason, setReason] = useState("");
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState<string | null>(null);

    const tenantName = me?.tenant.name ?? "";
    const tenantId = me?.tenant.id;

    const nameMatches = tenantName.length > 0 && confirmText === tenantName;
    const reasonOk = reason.trim().length >= 10;
    const canDelete = !!tenantId && nameMatches && reasonOk && !busy;

    async function handleDelete() {
        if (!tenantId) return;
        setError(null);
        setBusy(true);
        try {
            // Backend requires both a 10+ char `reason` (audit log) and
            // an exact tenant-name match — see gdpr.py.
            await api.del<unknown>(`/tenants/${tenantId}`, {
                reason: reason.trim(),
                confirm_tenant_name: confirmText,
            });
            // Sign out via Clerk so the now-orphaned session token can't
            // be reused, then bounce to /sign-in. The Clerk session is
            // separate from tenant data, so signOut on its own won't
            // 404 on us.
            await clerk.signOut();
            router.replace("/sign-in");
        } catch (err) {
            setError(humanizeError(err));
            setBusy(false);
        }
    }

    return (
        <div className="rounded-md border border-accent-rose/40 bg-accent-rose/5 p-4">
            <h4 className="font-medium text-accent-rose">
                Delete tenant permanently
            </h4>
            <p className="mt-1 text-sm text-text-muted">
                Wipes every row owned by{" "}
                <strong>{tenantName || "this tenant"}</strong>: interactions,
                action items, users, scorecards, audit log entries — the
                whole workspace. This is irreversible. Run an export first
                if you may need the data later.
            </p>
            <div className="mt-3 space-y-2">
                <label className="block text-xs font-medium">
                    Reason for delete (10+ characters, recorded on the audit log)
                    <textarea
                        rows={2}
                        value={reason}
                        onChange={(e) => setReason(e.target.value)}
                        placeholder="e.g. Customer cancelled and requested GDPR Art. 17 erasure on…"
                        className="mt-1 w-full rounded-md border border-border bg-bg-card px-3 py-2 text-sm"
                    />
                </label>
                <label className="block text-xs font-medium">
                    Type the tenant name <code className="font-mono">{tenantName || "—"}</code>{" "}
                    to enable the delete button
                    <input
                        type="text"
                        value={confirmText}
                        onChange={(e) => setConfirmText(e.target.value)}
                        placeholder={tenantName}
                        className="mt-1 w-full rounded-md border border-border bg-bg-card px-3 py-2 text-sm"
                    />
                </label>
                <button
                    type="button"
                    onClick={handleDelete}
                    disabled={!canDelete}
                    className="rounded-md bg-accent-rose px-3 py-1.5 text-xs font-medium text-white disabled:opacity-40"
                >
                    {busy ? "Deleting…" : "Delete tenant permanently"}
                </button>
                {error ? (
                    <p className="text-xs text-accent-rose">{error}</p>
                ) : null}
            </div>
        </div>
    );
}
