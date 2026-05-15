"use client";

import { useEffect, useRef, useState } from "react";
import clsx from "clsx";
import { useMe, useSetPreviewRole, type UserRole } from "@/lib/me";

const ROLE_OPTIONS: { value: UserRole; label: string }[] = [
    { value: "agent", label: "Agent" },
    { value: "manager", label: "Manager" },
    { value: "admin", label: "Admin" },
];

function roleLabel(role: UserRole): string {
    return role.charAt(0).toUpperCase() + role.slice(1);
}

/**
 * Sandbox-only role-preview switcher. Renders a dropdown pill in the
 * app-shell header that lets a single trial user preview the agent /
 * manager / admin views without seeding extra test users. Visible on
 * any sandbox-tier tenant (regardless of trial-active state, so the
 * affordance survives an expired-but-still-evaluating sandbox); the
 * backend applies the matching tier gate, so a non-sandbox tenant
 * could never honour a preview-role override even if the pill *were*
 * rendered.
 *
 * When the override is currently being applied, also renders a thin
 * status banner under the header inviting the user to switch back to
 * their real role.
 */
export function PreviewRolePill() {
    const { data } = useMe();
    const { mutate, isPending } = useSetPreviewRole();
    const [open, setOpen] = useState(false);
    const containerRef = useRef<HTMLDivElement | null>(null);

    // Close the menu on outside-click (mirrors the Clerk UserButton's
    // own dismiss behaviour so the two affordances feel consistent).
    useEffect(() => {
        if (!open) return;
        function onClick(e: MouseEvent) {
            const node = containerRef.current;
            if (node && !node.contains(e.target as Node)) {
                setOpen(false);
            }
        }
        window.addEventListener("mousedown", onClick);
        return () => window.removeEventListener("mousedown", onClick);
    }, [open]);

    if (!data) return null;
    const { tenant, user } = data;
    if (!user) return null;
    if (tenant.plan_tier !== "sandbox") return null;

    const currentPreview = user.preview_role;
    // Visual selection: prefer the stored preview_role (so the pill
    // stays accurate when the trial flag flips between requests); fall
    // back to the effective role.
    const checkedRole: UserRole = currentPreview ?? user.role;

    function selectRole(role: UserRole) {
        setOpen(false);
        if (role === currentPreview) return;
        mutate({ role });
    }

    return (
        <div ref={containerRef} className="relative">
            <button
                type="button"
                aria-haspopup="menu"
                aria-expanded={open}
                onClick={() => setOpen((v) => !v)}
                disabled={isPending}
                className={clsx(
                    "inline-flex items-center gap-1.5 rounded-full border border-border bg-bg-card px-3 py-1 text-xs font-medium text-text-muted transition-colors hover:bg-bg-card-hover",
                    isPending && "opacity-60",
                )}
            >
                <span aria-hidden="true" className="text-text-subtle">
                    View as
                </span>
                <span className="text-text">{roleLabel(checkedRole)}</span>
                <svg
                    width="10"
                    height="10"
                    viewBox="0 0 10 10"
                    fill="none"
                    aria-hidden="true"
                >
                    <path
                        d="M2 4l3 3 3-3"
                        stroke="currentColor"
                        strokeWidth="1.5"
                        strokeLinecap="round"
                        strokeLinejoin="round"
                    />
                </svg>
            </button>
            {open ? (
                <div
                    role="menu"
                    aria-label="Preview role"
                    className="absolute right-0 top-full z-20 mt-1 w-44 rounded-md border border-border bg-bg-card py-1 shadow-md"
                >
                    {ROLE_OPTIONS.map((opt) => {
                        const checked = opt.value === checkedRole;
                        return (
                            <button
                                key={opt.value}
                                role="menuitemradio"
                                aria-checked={checked}
                                type="button"
                                onClick={() => selectRole(opt.value)}
                                className={clsx(
                                    "flex w-full items-center justify-between px-3 py-1.5 text-left text-sm transition-colors",
                                    checked
                                        ? "text-text"
                                        : "text-text-muted hover:bg-bg-card-hover hover:text-text",
                                )}
                            >
                                <span>View as {opt.label}</span>
                                {checked ? (
                                    <svg
                                        width="12"
                                        height="12"
                                        viewBox="0 0 12 12"
                                        fill="none"
                                        aria-hidden="true"
                                    >
                                        <path
                                            d="M2 6.5l2.5 2.5L10 3.5"
                                            stroke="currentColor"
                                            strokeWidth="1.5"
                                            strokeLinecap="round"
                                            strokeLinejoin="round"
                                        />
                                    </svg>
                                ) : null}
                            </button>
                        );
                    })}
                </div>
            ) : null}
        </div>
    );
}

/**
 * Thin status banner shown when the sandbox preview overlay is
 * currently being applied. Click target clears the override so the
 * user can return to their real role with one click.
 */
export function PreviewRoleBanner() {
    const { data } = useMe();
    const { mutate, isPending } = useSetPreviewRole();
    if (!data) return null;
    const user = data.user;
    if (!user || !user.is_previewing) return null;

    return (
        <div className="mx-4 my-3 flex flex-wrap items-center gap-2 rounded-lg border border-primary/40 bg-primary-soft px-4 py-2 text-sm">
            <span>
                Preview mode — viewing as{" "}
                <strong className="capitalize">{user.role}</strong>.
            </span>
            <button
                type="button"
                onClick={() => mutate({ role: null })}
                disabled={isPending}
                className={clsx(
                    "rounded-md border border-primary/50 bg-bg-card px-2 py-0.5 text-xs font-medium text-primary transition-colors hover:bg-bg-card-hover",
                    isPending && "opacity-60",
                )}
            >
                Switch back to{" "}
                <span className="capitalize">{user.real_role}</span>
            </button>
        </div>
    );
}
