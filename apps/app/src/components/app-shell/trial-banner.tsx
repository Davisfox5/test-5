"use client";

import Link from "next/link";
import { useMe } from "@/lib/me";

function daysLeft(trialEndsAt: string): number {
    const diff = new Date(trialEndsAt).getTime() - Date.now();
    return Math.max(0, Math.ceil(diff / (24 * 60 * 60 * 1000)));
}

export function TrialBanner() {
    const { data } = useMe();
    if (!data) return null;
    const { tenant } = data;

    if (tenant.trial_expired) {
        return (
            <div className="mx-4 my-3 rounded-lg border border-accent-amber/60 bg-accent-amber/10 px-4 py-3 text-sm">
                <strong>Your sandbox trial has ended.</strong>{" "}
                Pick a plan to keep running calls through Linda.{" "}
                <Link href="/settings" className="underline">
                    See plans
                </Link>
            </div>
        );
    }
    if (tenant.trial_active && tenant.trial_ends_at) {
        return (
            <div className="mx-4 my-3 rounded-lg border border-primary/50 bg-primary-soft px-4 py-3 text-sm">
                <strong>{daysLeft(tenant.trial_ends_at)} days left</strong> on your
                sandbox trial.{" "}
                <Link href="/settings" className="underline">
                    Upgrade now
                </Link>{" "}
                to keep full access.
            </div>
        );
    }
    return null;
}
