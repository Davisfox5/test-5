"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";

/**
 * Trial signup — creates the sandbox tenant via POST /api/v1/trial/signup.
 * Clerk handles the underlying user identity via /sign-up; this page
 * collects the company name and links the two.
 */
export default function TrialSignupPage() {
    const router = useRouter();
    const [company, setCompany] = useState("");
    const [email, setEmail] = useState("");
    const [name, setName] = useState("");
    const [submitting, setSubmitting] = useState(false);
    const [error, setError] = useState<string | null>(null);

    async function onSubmit(event: React.FormEvent) {
        event.preventDefault();
        setSubmitting(true);
        setError(null);
        try {
            const resp = await fetch("/api/v1/trial/signup", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ company, email, name }),
            });
            if (!resp.ok) {
                const body = await resp.json().catch(() => null);
                throw new Error(body?.detail ?? `Signup failed (${resp.status})`);
            }
            router.push("/sign-up");  // hand off to Clerk to create the auth identity
        } catch (e) {
            setError(e instanceof Error ? e.message : "Signup failed");
            setSubmitting(false);
        }
    }

    return (
        <main className="mx-auto flex min-h-screen max-w-md flex-col justify-center gap-5 px-6">
            <div>
                <h1 className="text-2xl font-bold">Start your 14-day sandbox</h1>
                <p className="text-text-muted mt-1">
                    Bring your own calls, explore Linda end-to-end, and decide on a
                    plan when you&apos;re ready.
                </p>
            </div>
            <form onSubmit={onSubmit} className="flex flex-col gap-3">
                <Field label="Company name">
                    <input
                        required
                        className="w-full rounded-md border border-border bg-bg-card px-3 py-2"
                        value={company}
                        onChange={(e) => setCompany(e.target.value)}
                        autoComplete="organization"
                        minLength={2}
                    />
                </Field>
                <Field label="Your name">
                    <input
                        className="w-full rounded-md border border-border bg-bg-card px-3 py-2"
                        value={name}
                        onChange={(e) => setName(e.target.value)}
                        autoComplete="name"
                    />
                </Field>
                <Field label="Work email">
                    <input
                        required
                        type="email"
                        className="w-full rounded-md border border-border bg-bg-card px-3 py-2"
                        value={email}
                        onChange={(e) => setEmail(e.target.value)}
                        autoComplete="email"
                    />
                </Field>
                <button
                    type="submit"
                    disabled={submitting}
                    className="btn btn-primary mt-2 disabled:opacity-60"
                >
                    {submitting ? "Creating your workspace…" : "Start sandbox"}
                </button>
                {error && <p className="text-sm text-accent-rose">{error}</p>}
            </form>
        </main>
    );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
    return (
        <label className="flex flex-col gap-1 text-sm">
            <span className="text-text-muted">{label}</span>
            {children}
        </label>
    );
}
