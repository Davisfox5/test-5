import { Sidebar } from "@/components/app-shell/sidebar";
import { Header } from "@/components/app-shell/header";
import { TrialBanner } from "@/components/app-shell/trial-banner";

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
