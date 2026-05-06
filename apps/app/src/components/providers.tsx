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
                        staleTime: 30 * 1000,
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
