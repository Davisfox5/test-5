"use client";

import {
    createContext,
    useCallback,
    useContext,
    useEffect,
    useMemo,
    useState,
    type ReactNode,
} from "react";

/**
 * Context Drawer — a single right-side slide-out panel that any
 * component can populate. Triggers include:
 *  - Action item "Jump to source" → transcript section content
 *  - Topic chip click → KB articles + related calls
 *  - Customer Behavior Radar axis click → driving quotes
 *  - "View related action items" → list of related items
 *
 * One drawer, variable content. Persistable across route changes so
 * navigating from the interaction page to the customer page doesn't
 * blow away the rep's open context.
 */

interface DrawerContent {
    title: string;
    body: ReactNode;
    /** Optional close-handler hook for content that owns its lifecycle. */
    onClose?: () => void;
}

interface ContextDrawerContextValue {
    isOpen: boolean;
    content: DrawerContent | null;
    open: (content: DrawerContent) => void;
    close: () => void;
}

const ContextDrawerContext = createContext<ContextDrawerContextValue | null>(null);

export function useContextDrawer(): ContextDrawerContextValue {
    const ctx = useContext(ContextDrawerContext);
    if (!ctx) {
        throw new Error(
            "useContextDrawer must be used inside a <ContextDrawerProvider>",
        );
    }
    return ctx;
}

export function ContextDrawerProvider({ children }: { children: ReactNode }) {
    const [content, setContent] = useState<DrawerContent | null>(null);

    const close = useCallback(() => {
        if (content?.onClose) content.onClose();
        setContent(null);
    }, [content]);

    const open = useCallback((next: DrawerContent) => {
        setContent(next);
    }, []);

    // Close on Escape — standard accessibility expectation for a drawer.
    useEffect(() => {
        if (!content) return;
        function onKey(e: KeyboardEvent) {
            if (e.key === "Escape") close();
        }
        window.addEventListener("keydown", onKey);
        return () => window.removeEventListener("keydown", onKey);
    }, [content, close]);

    const value = useMemo<ContextDrawerContextValue>(
        () => ({
            isOpen: Boolean(content),
            content,
            open,
            close,
        }),
        [content, open, close],
    );

    return (
        <ContextDrawerContext.Provider value={value}>
            {children}
            {content && <DrawerView content={content} onClose={close} />}
        </ContextDrawerContext.Provider>
    );
}

function DrawerView({
    content,
    onClose,
}: {
    content: DrawerContent;
    onClose: () => void;
}) {
    return (
        <>
            {/* Backdrop — clicking dismisses. Subtle so the rep can still
                see the underlying surface (the drawer is for context, not
                a takeover). */}
            <div
                className="fixed inset-0 z-40 bg-black/30 backdrop-blur-[2px]"
                onClick={onClose}
                aria-hidden
            />
            <aside
                role="dialog"
                aria-label={content.title}
                className="fixed right-0 top-0 z-50 flex h-full w-full max-w-md flex-col border-l border bg-card shadow-xl"
            >
                <header className="flex items-center justify-between border-b border px-4 py-3">
                    <h2 className="text-base font-semibold text-text">
                        {content.title}
                    </h2>
                    <button
                        type="button"
                        onClick={onClose}
                        aria-label="Close"
                        className="rounded-md p-1 text-text-muted hover:bg-card-hover hover:text-text focus:outline-none focus:ring-2 focus:ring-primary"
                    >
                        ✕
                    </button>
                </header>
                <div className="flex-1 overflow-y-auto px-4 py-4">
                    {content.body}
                </div>
            </aside>
        </>
    );
}
