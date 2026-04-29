import { Sidebar } from "@/components/app-shell/sidebar";
import { Header } from "@/components/app-shell/header";
import { TrialBanner } from "@/components/app-shell/trial-banner";

// Authenticated app shell — every route under (app) is gated on a Clerk
// session and the per-tenant /api/v1/me payload, so static prerender is
// always wrong (and historically blew up at build time when the Clerk
// publishable key wasn't passed as a Docker build arg).
export const dynamic = "force-dynamic";

export default function AppLayout({ children }: { children: React.ReactNode }) {
    return (
        <div className="flex min-h-screen">
            <Sidebar />
            <div className="flex flex-1 flex-col">
                <Header />
                <TrialBanner />
                <main className="flex-1 px-6 py-4">{children}</main>
            </div>
        </div>
    );
}
