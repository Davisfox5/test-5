"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { useUser } from "@clerk/nextjs";

const STASH_KEY = "linda-trial-signup";

type StashedSignup = {
    name: string;
    email: string;
    company: string;
    role: string;
    companySize: string;
    useCase: string;
};

export default function TrialSignupCompletePage() {
    const router = useRouter();
    const { user, isLoaded } = useUser();
    const [error, setError] = useState<string | null>(null);
    const ranRef = useRef(false);

    useEffect(() => {
        if (!isLoaded) return;
        if (!user) {
            router.replace("/sign-up?redirect_url=/signup/complete");
            return;
        }
        if (ranRef.current) return;
        ranRef.current = true;

        (async () => {
            const stashedRaw = (() => {
                try {
                    return sessionStorage.getItem(STASH_KEY);
                } catch {
                    return null;
                }
            })();

            const fallback: StashedSignup = {
                name:
                    user.fullName ||
                    [user.firstName, user.lastName].filter(Boolean).join(" ") ||
                    "",
                email: user.primaryEmailAddress?.emailAddress || "",
                company: "",
                role: "",
                companySize: "",
                useCase: "",
            };
            const stashed: StashedSignup = stashedRaw
                ? { ...fallback, ...(JSON.parse(stashedRaw) as StashedSignup) }
                : fallback;

            if (!stashed.company) {
                router.replace("/signup");
                return;
            }

            try {
                const resp = await fetch("/api/v1/trial/signup", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        email: stashed.email || fallback.email,
                        name: stashed.name || fallback.name,
                        company: stashed.company,
                        clerk_user_id: user.id,
                        role: stashed.role || undefined,
                        company_size: stashed.companySize || undefined,
                        use_case: stashed.useCase || undefined,
                    }),
                });
                if (!resp.ok) {
                    const body = await resp.json().catch(() => null);
                    throw new Error(body?.detail ?? `Signup failed (${resp.status})`);
                }
                try {
                    sessionStorage.removeItem(STASH_KEY);
                } catch {
                    /* ignore */
                }
                router.replace("/dashboard");
            } catch (e) {
                setError(e instanceof Error ? e.message : "Could not finish signup");
            }
        })();
    }, [isLoaded, router, user]);

    return (
        <main className="mx-auto flex min-h-screen max-w-md flex-col justify-center gap-3 px-6 text-center">
            {error ? (
                <>
                    <h1 className="text-xl font-bold">We couldn&apos;t finish setting up your sandbox</h1>
                    <p className="text-text-muted">{error}</p>
                    <a className="btn btn-primary mt-4" href="/signup">
                        Try again
                    </a>
                </>
            ) : (
                <>
                    <h1 className="text-xl font-bold">Setting up your sandbox…</h1>
                    <p className="text-text-muted">
                        Provisioning your workspace — this takes a few seconds.
                    </p>
                </>
            )}
        </main>
    );
}
