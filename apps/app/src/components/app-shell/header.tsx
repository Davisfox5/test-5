"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import clsx from "clsx";
import { UserButton } from "@clerk/nextjs";
import { useMe } from "@/lib/me";
import { LindaMark } from "@/components/brand/linda-mark";
import { navItemsForRole } from "@/components/app-shell/sidebar";
import type { UserRole } from "@/lib/me";

export function Header() {
    const { data } = useMe();
    const [drawerOpen, setDrawerOpen] = useState(false);
    const pathname = usePathname();
    const role: UserRole = data?.user?.role ?? "agent";
    const items = navItemsForRole(role);

    // Close the drawer whenever the route changes — Next.js doesn't do
    // this for us because we're not unmounting the layout.
    useEffect(() => {
        setDrawerOpen(false);
    }, [pathname]);

    return (
        <>
            <header className="sticky top-0 z-10 flex h-14 items-center justify-between border-b border-border bg-bg-main/80 px-4 backdrop-blur md:px-6">
                <div className="flex items-center gap-3">
                    <button
                        type="button"
                        aria-label="Open navigation menu"
                        aria-expanded={drawerOpen}
                        onClick={() => setDrawerOpen(true)}
                        className="md:hidden inline-flex h-9 w-9 items-center justify-center rounded-md border border-border bg-bg-card text-text-muted hover:bg-bg-card-hover"
                    >
                        {/* hamburger glyph */}
                        <svg
                            width="18"
                            height="18"
                            viewBox="0 0 18 18"
                            fill="none"
                            aria-hidden="true"
                        >
                            <path
                                d="M3 5h12M3 9h12M3 13h12"
                                stroke="currentColor"
                                strokeWidth="1.5"
                                strokeLinecap="round"
                            />
                        </svg>
                    </button>
                    <div>
                        <h1 className="text-sm font-semibold">
                            {data?.tenant.name ?? "LINDA"}
                        </h1>
                        <p className="text-xs text-text-subtle capitalize">
                            {data?.tenant.plan_tier ?? ""} plan
                        </p>
                    </div>
                </div>
                <UserButton afterSignOutUrl="/" />
            </header>

            {/* Mobile nav drawer — only rendered when open so the
                backdrop doesn't intercept clicks on desktop. */}
            {drawerOpen ? (
                <div
                    className="fixed inset-0 z-30 md:hidden"
                    role="dialog"
                    aria-modal="true"
                    aria-label="Primary navigation"
                >
                    <button
                        type="button"
                        aria-label="Close navigation menu"
                        onClick={() => setDrawerOpen(false)}
                        className="absolute inset-0 bg-black/50"
                    />
                    <aside className="relative flex h-full w-64 max-w-[80vw] flex-col border-r border-border bg-bg-secondary shadow-xl">
                        <div className="flex items-center justify-between gap-2 border-b border-border px-5 py-4">
                            <div className="flex items-center gap-2">
                                <LindaMark size={26} />
                                <span className="text-lg font-black tracking-wide">
                                    LINDA
                                </span>
                            </div>
                            <button
                                type="button"
                                aria-label="Close navigation menu"
                                onClick={() => setDrawerOpen(false)}
                                className="rounded-md p-1 text-text-muted hover:bg-bg-card-hover"
                            >
                                <svg
                                    width="18"
                                    height="18"
                                    viewBox="0 0 18 18"
                                    fill="none"
                                    aria-hidden="true"
                                >
                                    <path
                                        d="M4 4l10 10M14 4L4 14"
                                        stroke="currentColor"
                                        strokeWidth="1.5"
                                        strokeLinecap="round"
                                    />
                                </svg>
                            </button>
                        </div>
                        <nav
                            className="flex flex-col gap-1 p-3"
                            aria-label="Primary"
                        >
                            {items.map((item) => {
                                const active = pathname?.startsWith(item.href);
                                return (
                                    <Link
                                        key={item.href}
                                        href={item.href}
                                        className={clsx(
                                            "rounded-md px-3 py-2 text-sm transition-colors",
                                            active
                                                ? "bg-primary-soft text-primary"
                                                : "text-text-muted hover:bg-bg-card-hover hover:text-text",
                                        )}
                                    >
                                        {item.label}
                                    </Link>
                                );
                            })}
                        </nav>
                        <div className="mt-auto px-5 py-3 text-xs text-text-subtle">
                            Role:{" "}
                            <span className="font-semibold capitalize">
                                {role}
                            </span>
                        </div>
                    </aside>
                </div>
            ) : null}
        </>
    );
}
