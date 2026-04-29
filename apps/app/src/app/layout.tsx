import type { Metadata } from "next";
import { ClerkProvider } from "@clerk/nextjs";
import { Providers } from "@/components/providers";
import "./globals.css";

export const metadata: Metadata = {
    title: "LINDA",
    description:
        "Linda listens to every call so you don't have to. She turns conversations into summaries, coaching moments, and follow-ups.",
};

// Every page in this SPA renders inside <ClerkProvider> and reads
// runtime auth state — static prerender is never the right behavior,
// and historically blew up the Docker build when the publishable key
// wasn't passed as a build arg. Opt the entire app out of SSG.
export const dynamic = "force-dynamic";

export default function RootLayout({ children }: { children: React.ReactNode }) {
    return (
        <ClerkProvider>
            <html lang="en">
                <body>
                    <Providers>{children}</Providers>
                </body>
            </html>
        </ClerkProvider>
    );
}
