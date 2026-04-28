"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import clsx from "clsx";
import { LindaMark } from "@/components/brand/linda-mark";
import { useMe } from "@/lib/me";
import type { UserRole } from "@/lib/me";

type NavItem = {
    href: string;
    label: string;
    minRole: UserRole;
};

const ROLE_RANK: Record<UserRole, number> = { agent: 1, manager: 2, admin: 3 };

// Single source of truth for the nav list — both the desktop sidebar
// and the mobile drawer (in `header.tsx`) consume this so they can't
// drift out of sync.
export const NAV: NavItem[] = [
    { href: "/dashboard", label: "Dashboard", minRole: "agent" },
    { href: "/interactions", label: "Interactions", minRole: "agent" },
    { href: "/action-items", label: "Action Items", minRole: "agent" },
    { href: "/scorecards", label: "Scorecards", minRole: "agent" },
    { href: "/team", label: "Team", minRole: "manager" },
    { href: "/analytics", label: "Analytics", minRole: "manager" },
    { href: "/billing", label: "Billing & plan", minRole: "admin" },
    { href: "/settings", label: "Settings", minRole: "agent" },
];

export function navItemsForRole(role: UserRole): NavItem[] {
    return NAV.filter((item) => ROLE_RANK[role] >= ROLE_RANK[item.minRole]);
}

export function Sidebar() {
    const pathname = usePathname();
    const { data } = useMe();
    const role: UserRole = data?.user?.role ?? "agent";
    const items = navItemsForRole(role);

    return (
        <aside className="sticky top-0 hidden h-screen w-60 shrink-0 border-r border-border bg-bg-secondary md:block">
            <div className="flex items-center gap-2 px-5 py-4 border-b border-border">
                <LindaMark size={26} />
                <span className="text-lg font-black tracking-wide">LINDA</span>
            </div>
            <nav className="flex flex-col gap-1 p-3" aria-label="Primary">
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
                Role: <span className="font-semibold capitalize">{role}</span>
            </div>
        </aside>
    );
}
