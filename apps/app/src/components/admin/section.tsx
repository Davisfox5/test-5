import * as React from "react";

export function Section({
    title,
    subtitle,
    actions,
    children,
}: {
    title: string;
    subtitle?: string;
    actions?: React.ReactNode;
    children: React.ReactNode;
}) {
    return (
        <section className="rounded-lg border border-border bg-bg-card p-6">
            <div className="mb-4 flex items-start justify-between gap-4">
                <div>
                    <h3 className="text-lg font-semibold">{title}</h3>
                    {subtitle ? (
                        <p className="text-sm text-text-muted mt-1">
                            {subtitle}
                        </p>
                    ) : null}
                </div>
                {actions ? (
                    <div className="shrink-0 flex items-center gap-2">
                        {actions}
                    </div>
                ) : null}
            </div>
            {children}
        </section>
    );
}

export function AdminGate({
    role,
    children,
}: {
    role: string | undefined;
    children: React.ReactNode;
}) {
    if (role === "admin") return <>{children}</>;
    return (
        <div className="rounded-md border border-accent-amber/40 bg-accent-amber/10 p-4 text-sm text-text-muted">
            Admin access required to view or change this section.
        </div>
    );
}

export function ManagerGate({
    role,
    children,
}: {
    role: string | undefined;
    children: React.ReactNode;
}) {
    if (role === "admin" || role === "manager") return <>{children}</>;
    return (
        <div className="rounded-md border border-accent-amber/40 bg-accent-amber/10 p-4 text-sm text-text-muted">
            Manager or admin access required to view this page.
        </div>
    );
}

export function SkeletonCard() {
    return (
        <div className="rounded-lg border border-border bg-bg-card p-6 animate-pulse">
            <div className="h-4 w-40 bg-bg-raised rounded mb-3" />
            <div className="h-3 w-64 bg-bg-raised rounded" />
        </div>
    );
}

export function ErrorCard({ message }: { message: string }) {
    return (
        <div className="rounded-lg border border-accent-rose/60 bg-accent-rose/10 text-text-main p-4 text-sm">
            {message}
        </div>
    );
}

export function humanizeError(error: unknown): string {
    if (error instanceof Error) return error.message;
    return "Unexpected error — see console for details.";
}
