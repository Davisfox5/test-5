"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";
import { ContextDrawerProvider } from "@/components/context-drawer/context-drawer";

export function Providers({ children }: { children: React.ReactNode }) {
    const [queryClient] = useState(
        () =>
            new QueryClient({
                defaultOptions: {
                    queries: {
                        // 5 minutes — most read endpoints don't change
                        // meaningfully between user clicks, and a 30s
                        // staleTime was firing a refetch on practically
                        // every page nav. Pages that genuinely need
                        // tighter freshness pass their own staleTime.
                        staleTime: 5 * 60 * 1000,
                        refetchOnWindowFocus: false,
                    },
                },
            }),
    );
    return (
        <QueryClientProvider client={queryClient}>
            <ContextDrawerProvider>{children}</ContextDrawerProvider>
        </QueryClientProvider>
    );
}
