import type { Metadata } from "next";
import { ClerkProvider } from "@clerk/nextjs";
import { Providers } from "@/components/providers";
import "./globals.css";

export const metadata: Metadata = {
    title: "LINDA",
    description:
        "Linda listens to every call so you don't have to. She turns conversations into summaries, coaching moments, and follow-ups.",
};

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
