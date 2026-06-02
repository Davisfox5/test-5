"use client";

/** Shared row helpers for the customer list views. */

import Image from "next/image";
import Link from "next/link";
import { useMemo } from "react";
import { faviconFor, type CustomerListItem } from "@/lib/customers";

export function CustomerLogo({
    domain,
    name,
    size = 32,
}: {
    domain: string | null;
    name: string;
    size?: number;
}) {
    const fav = faviconFor(domain);
    const initials = useMemo(() => {
        const tokens = name
            .split(/\s+/)
            .filter(Boolean)
            .slice(0, 2)
            .map((t) => t[0]?.toUpperCase() ?? "");
        return tokens.join("") || "·";
    }, [name]);

    if (fav) {
        return (
            <div
                className="overflow-hidden rounded-md bg-bg-secondary"
                style={{ width: size, height: size }}
            >
                <Image
                    src={fav}
                    alt={`${domain ?? name} logo`}
                    width={size}
                    height={size}
                    className="object-cover"
                    unoptimized={false}
                    loading="lazy"
                />
            </div>
        );
    }
    return (
        <div
            className="flex items-center justify-center rounded-md bg-primary-soft text-xs font-semibold text-primary"
            style={{ width: size, height: size }}
        >
            {initials}
        </div>
    );
}

export function OwnerStack({
    owners,
}: {
    owners: CustomerListItem["owners"];
}) {
    if (owners.length === 0) {
        return <span className="text-xs text-text-subtle">unassigned</span>;
    }
    const visible = owners.slice(0, 3);
    const extra = owners.length - visible.length;
    return (
        <div className="flex items-center">
            {visible.map((o, idx) => (
                <div
                    key={o.user_id}
                    title={`${o.name ?? o.email ?? "Unknown"} (${o.role})`}
                    className={`flex h-7 w-7 items-center justify-center rounded-full border-2 border-bg-card text-xs font-medium ${
                        o.role === "primary"
                            ? "bg-primary text-white"
                            : "bg-bg-secondary text-text-muted"
                    } ${idx > 0 ? "-ml-2" : ""}`}
                >
                    {(o.name || o.email || "?").charAt(0).toUpperCase()}
                </div>
            ))}
            {extra > 0 ? (
                <div className="-ml-2 flex h-7 w-7 items-center justify-center rounded-full border-2 border-bg-card bg-bg-secondary text-xs text-text-muted">
                    +{extra}
                </div>
            ) : null}
        </div>
    );
}

export function ChurnTone(value: number | null): {
    label: string;
    cls: string;
} {
    if (value == null) return { label: "-", cls: "text-text-subtle" };
    const pct = `${Math.round(value * 100)}%`;
    if (value >= 0.7) return { label: pct, cls: "text-accent-rose" };
    if (value >= 0.4) return { label: pct, cls: "text-accent-amber" };
    return { label: pct, cls: "text-accent-emerald" };
}

export function CustomerCardLink({
    c,
    children,
    className = "",
}: {
    c: CustomerListItem;
    children: React.ReactNode;
    className?: string;
}) {
    return (
        <Link
            href={`/customers/${c.id}`}
            className={`block rounded-lg border border-border bg-bg-card p-4 hover:bg-bg-card-hover ${className}`}
        >
            {children}
        </Link>
    );
}
