"use client";

import { UserButton } from "@clerk/nextjs";
import { useMe } from "@/lib/me";

export function Header() {
    const { data } = useMe();
    return (
        <header className="sticky top-0 z-10 flex h-14 items-center justify-between border-b border-border bg-bg-main/80 px-6 backdrop-blur">
            <div>
                <h1 className="text-sm font-semibold">
                    {data?.tenant.name ?? "LINDA"}
                </h1>
                <p className="text-xs text-text-subtle capitalize">
                    {data?.tenant.plan_tier ?? ""} plan
                </p>
            </div>
            <UserButton afterSignOutUrl="/" />
        </header>
    );
}
