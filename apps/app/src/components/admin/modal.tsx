"use client";

import * as React from "react";

export function Modal({
    open,
    onClose,
    title,
    children,
}: {
    open: boolean;
    onClose: () => void;
    title: string;
    children: React.ReactNode;
}) {
    React.useEffect(() => {
        if (!open) return;
        const onKey = (e: KeyboardEvent) => {
            if (e.key === "Escape") onClose();
        };
        window.addEventListener("keydown", onKey);
        return () => window.removeEventListener("keydown", onKey);
    }, [open, onClose]);

    if (!open) return null;
    return (
        <div
            className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
            onClick={onClose}
            role="presentation"
        >
            <div
                className="w-full max-w-lg rounded-lg border border-border bg-bg-card shadow-xl"
                onClick={(e) => e.stopPropagation()}
                role="dialog"
                aria-modal="true"
                aria-label={title}
            >
                <div className="flex items-center justify-between border-b border-border px-5 py-3">
                    <h3 className="text-base font-semibold">{title}</h3>
                    <button
                        type="button"
                        onClick={onClose}
                        className="text-text-subtle hover:text-text-main"
                        aria-label="Close"
                    >
                        ×
                    </button>
                </div>
                <div className="p-5">{children}</div>
            </div>
        </div>
    );
}
