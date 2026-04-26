"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";

const STASH_KEY = "linda-trial-signup";

type StashedSignup = {
    name: string;
    email: string;
    company: string;
    role: string;
    companySize: string;
    useCase: string;
};

export default function TrialSignupPage() {
    const router = useRouter();
    const [form, setForm] = useState<StashedSignup>({
        name: "",
        email: "",
        company: "",
        role: "",
        companySize: "",
        useCase: "",
    });
    const [submitting, setSubmitting] = useState(false);
    const [error, setError] = useState<string | null>(null);

    function update<K extends keyof StashedSignup>(key: K, value: string) {
        setForm((prev) => ({ ...prev, [key]: value }));
    }

    function onSubmit(event: React.FormEvent) {
        event.preventDefault();
        setError(null);
        setSubmitting(true);
        try {
            sessionStorage.setItem(STASH_KEY, JSON.stringify(form));
        } catch {
            setError("Your browser blocked session storage. Please enable it and try again.");
            setSubmitting(false);
            return;
        }
        const params = new URLSearchParams({
            redirect_url: "/signup/complete",
        });
        if (form.email) params.set("email_address", form.email);
        router.push(`/sign-up?${params.toString()}`);
    }

    return (
        <main className="mx-auto flex min-h-screen max-w-xl flex-col justify-center gap-5 px-6 py-12">
            <div>
                <h1 className="text-2xl font-bold">Start your 14-day sandbox</h1>
                <p className="text-text-muted mt-1">
                    Bring your own calls, explore Linda end-to-end, and decide on a
                    plan when you&apos;re ready. We&apos;ll create your workspace
                    after you confirm your email on the next step.
                </p>
            </div>
            <form onSubmit={onSubmit} className="flex flex-col gap-3">
                <Field label="Full name" required>
                    <input
                        required
                        className="w-full rounded-md border border-border bg-bg-card px-3 py-2"
                        value={form.name}
                        onChange={(e) => update("name", e.target.value)}
                        autoComplete="name"
                        minLength={2}
                    />
                </Field>
                <Field label="Work email" required>
                    <input
                        required
                        type="email"
                        className="w-full rounded-md border border-border bg-bg-card px-3 py-2"
                        value={form.email}
                        onChange={(e) => update("email", e.target.value)}
                        autoComplete="email"
                    />
                </Field>
                <Field label="Company / organization" required>
                    <input
                        required
                        className="w-full rounded-md border border-border bg-bg-card px-3 py-2"
                        value={form.company}
                        onChange={(e) => update("company", e.target.value)}
                        autoComplete="organization"
                        minLength={2}
                    />
                </Field>
                <Field label="Role / title" required>
                    <input
                        required
                        className="w-full rounded-md border border-border bg-bg-card px-3 py-2"
                        value={form.role}
                        onChange={(e) => update("role", e.target.value)}
                        autoComplete="organization-title"
                    />
                </Field>
                <Field label="Company size">
                    <select
                        className="w-full rounded-md border border-border bg-bg-card px-3 py-2"
                        value={form.companySize}
                        onChange={(e) => update("companySize", e.target.value)}
                    >
                        <option value="">Choose one (optional)</option>
                        <option value="1-10">1–10</option>
                        <option value="11-50">11–50</option>
                        <option value="51-200">51–200</option>
                        <option value="201-1000">201–1,000</option>
                        <option value="1000+">1,000+</option>
                    </select>
                </Field>
                <Field label="Primary use case">
                    <select
                        className="w-full rounded-md border border-border bg-bg-card px-3 py-2"
                        value={form.useCase}
                        onChange={(e) => update("useCase", e.target.value)}
                    >
                        <option value="">Choose one (optional)</option>
                        <option value="sales">Sales calls</option>
                        <option value="support">Customer support</option>
                        <option value="quality">Quality assurance / coaching</option>
                        <option value="research">Research / discovery</option>
                        <option value="other">Other</option>
                    </select>
                </Field>
                <button
                    type="submit"
                    disabled={submitting}
                    className="btn btn-primary mt-2 disabled:opacity-60"
                >
                    {submitting ? "Continuing…" : "Continue to email verification"}
                </button>
                {error && <p className="text-sm text-accent-rose">{error}</p>}
                <p className="text-text-subtle text-xs">
                    By continuing you agree to LINDA&apos;s terms and privacy policy.
                </p>
            </form>
        </main>
    );
}

function Field({
    label,
    required,
    children,
}: {
    label: string;
    required?: boolean;
    children: React.ReactNode;
}) {
    return (
        <label className="flex flex-col gap-1 text-sm">
            <span className="text-text-muted">
                {label}
                {required ? " *" : ""}
            </span>
            {children}
        </label>
    );
}
