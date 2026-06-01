"use client";

/**
 * Public CSAT survey.
 *
 * Mounted at /csat/[token]. No auth: the token in the URL is signed
 * with the tenant's CSAT secret on the backend and validates per-call.
 * One-shot form: pick 1-5, optionally leave a comment, submit. Renders
 * a polite "thanks" + closed state on success or if already submitted.
 */

import { useParams } from "next/navigation";
import { useEffect, useState } from "react";

interface PublicCase {
    case_subject: string;
    opened_at: string;
    resolved_at: string | null;
    status: string;
    already_submitted: boolean;
}

export default function CsatPublicPage() {
    const params = useParams();
    const token =
        typeof params.token === "string" ? params.token : params.token?.[0] || "";

    const [info, setInfo] = useState<PublicCase | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [loading, setLoading] = useState(true);
    const [score, setScore] = useState<number | null>(null);
    const [comment, setComment] = useState("");
    const [submitting, setSubmitting] = useState(false);
    const [done, setDone] = useState(false);

    useEffect(() => {
        let cancelled = false;
        (async () => {
            try {
                const resp = await fetch(`/api/v1/csat/${encodeURIComponent(token)}`);
                if (!resp.ok) {
                    if (resp.status === 404) {
                        setError(
                            "That survey link is invalid or has expired.",
                        );
                    } else {
                        setError(`Could not load survey (HTTP ${resp.status}).`);
                    }
                    return;
                }
                const body = (await resp.json()) as PublicCase;
                if (!cancelled) {
                    setInfo(body);
                    if (body.already_submitted) setDone(true);
                }
            } catch (err) {
                if (!cancelled) {
                    setError(
                        err instanceof Error ? err.message : "Network error",
                    );
                }
            } finally {
                if (!cancelled) setLoading(false);
            }
        })();
        return () => {
            cancelled = true;
        };
    }, [token]);

    const submit = async (e: React.FormEvent) => {
        e.preventDefault();
        if (score === null) return;
        setSubmitting(true);
        setError(null);
        try {
            const resp = await fetch(
                `/api/v1/csat/${encodeURIComponent(token)}`,
                {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        score,
                        comment: comment.trim() || undefined,
                    }),
                },
            );
            if (!resp.ok) {
                const detail = await resp.text();
                setError(
                    `Could not submit (${resp.status}): ${detail || "unknown error"}`,
                );
                return;
            }
            setDone(true);
        } catch (err) {
            setError(err instanceof Error ? err.message : "Network error");
        } finally {
            setSubmitting(false);
        }
    };

    return (
        <main className="mx-auto flex min-h-screen max-w-lg flex-col justify-center px-4">
            <div className="rounded-lg border border-border bg-bg-card p-6 shadow-sm">
                <h1 className="text-xl font-semibold">How did we do?</h1>
                {loading ? (
                    <p className="mt-3 text-sm text-text-muted">Loading…</p>
                ) : error ? (
                    <p className="mt-3 text-sm text-error">{error}</p>
                ) : !info ? null : done ? (
                    <div className="mt-3 space-y-2 text-sm">
                        <p>
                            Thanks. Your response was recorded for case{" "}
                            <strong>{info.case_subject}</strong>.
                        </p>
                        <p className="text-text-muted">
                            You can close this window.
                        </p>
                    </div>
                ) : (
                    <form onSubmit={submit} className="mt-4 space-y-4">
                        <p className="text-sm text-text-muted">
                            On a scale of 1 (poor) to 5 (excellent), how
                            satisfied are you with how we handled this case:{" "}
                            <strong className="text-text">
                                {info.case_subject}
                            </strong>
                            ?
                        </p>
                        <div className="flex justify-between gap-2">
                            {[1, 2, 3, 4, 5].map((n) => (
                                <button
                                    key={n}
                                    type="button"
                                    onClick={() => setScore(n)}
                                    className={
                                        "flex-1 rounded border px-3 py-3 text-lg font-semibold transition " +
                                        (score === n
                                            ? "border-primary bg-primary text-bg"
                                            : "border-border bg-bg hover:bg-bg-card-hover")
                                    }
                                    aria-pressed={score === n}
                                >
                                    {n}
                                </button>
                            ))}
                        </div>
                        <label className="block text-sm">
                            Anything else? (optional)
                            <textarea
                                value={comment}
                                onChange={(e) => setComment(e.target.value)}
                                rows={3}
                                maxLength={2000}
                                className="mt-1 w-full rounded border border-border bg-bg-card px-3 py-2 text-sm"
                            />
                        </label>
                        <button
                            type="submit"
                            disabled={score === null || submitting}
                            className="w-full rounded bg-primary px-4 py-2 text-sm font-semibold text-bg disabled:opacity-50"
                        >
                            {submitting ? "Submitting…" : "Submit"}
                        </button>
                    </form>
                )}
            </div>
        </main>
    );
}
