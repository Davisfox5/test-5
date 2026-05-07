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
const KNOWN_ROLES = new Set<string>(Object.keys(ROLE_RANK));

// Coerce whatever shape the backend returns into a known UserRole.
// Falls back to "agent" if the field is missing, null, or carries an
// unexpected value — without this, an unrecognized role bypassed every
// rank comparison (`undefined >= 1` is `false`) and emptied the entire
// nav. Initial render uses the default before /me resolves, so the
// sidebar is never empty during a hydration race.
function normalizeRole(raw: unknown): UserRole {
    return typeof raw === "string" && KNOWN_ROLES.has(raw)
        ? (raw as UserRole)
        : "agent";
}

// Surfaced as a tooltip on the sidebar role badge so non-admins
// understand why some nav items are missing for them.
const ROLE_TOOLTIPS: Record<UserRole, string> = {
    agent: "Agents see their own calls, action items, and scorecards.",
    manager: "Managers see team-level analytics + everything agents see.",
    admin: "Admins manage billing, integrations, team, and tenant settings.",
};

function formatRole(role: UserRole): string {
    // Capitalize the first letter only — `text-transform: capitalize`
    // upper-cases every word, which would mangle multi-word roles if
    // they're ever introduced.
    return role.charAt(0).toUpperCase() + role.slice(1);
}

// Single source of truth for the nav list — both the desktop sidebar
// and the mobile drawer (in `header.tsx`) consume this so they can't
// drift out of sync.
export const NAV: NavItem[] = [
    { href: "/dashboard", label: "Dashboard", minRole: "agent" },
    // Customers is the spine of the app shell. Per the plan, the
    // global "Interactions" feed has been demoted to a sibling tab on
    // /customers (``?tab=all-interactions``) so analysts work from the
    // account, not from a flat call list. /interactions/{id} detail
    // pages still exist and stay reachable.
    { href: "/customers", label: "Customers", minRole: "agent" },
    { href: "/action-items", label: "Action Items", minRole: "agent" },
    // Live coaching sits between items and communications: it's the
    // synchronous companion to the async follow-up workflow, and the
    // ordering reads as items → coaching → communications.
    { href: "/coaching", label: "Live Coaching", minRole: "manager" },
    // Action items become emails — slot the outbox right after items so
    // the workflow reads top-to-bottom. Manager+ only: agents shouldn't
    // see the whole tenant's outgoing email history.
    { href: "/communications", label: "Communications", minRole: "manager" },
    { href: "/scorecards", label: "Scorecards", minRole: "agent" },
    {
        href: "/knowledge-base",
        label: "Knowledge Base",
        minRole: "manager",
    },
    { href: "/team", label: "Team", minRole: "manager" },
    { href: "/manager-dashboard", label: "Team Dashboard", minRole: "manager" },
    { href: "/analytics", label: "Analytics", minRole: "manager" },
    { href: "/billing", label: "Billing & plan", minRole: "admin" },
    { href: "/settings", label: "Settings", minRole: "agent" },
];

export function navItemsForRole(role: UserRole): NavItem[] {
    const rank = ROLE_RANK[role] ?? ROLE_RANK.agent;
    return NAV.filter((item) => rank >= ROLE_RANK[item.minRole]);
}

export function Sidebar() {
    const pathname = usePathname();
    const { data } = useMe();
    const role = normalizeRole(data?.user?.role);
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
                Role:{" "}
                <span
                    className="font-semibold cursor-help"
                    title={ROLE_TOOLTIPS[role]}
                >
                    {formatRole(role)}
                </span>
            </div>
        </aside>
    );
}
