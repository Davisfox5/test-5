import Link from "next/link";
import { auth } from "@clerk/nextjs/server";
import { redirect } from "next/navigation";
import { LindaWordmark } from "@/components/brand/linda-wordmark";

export default async function LandingPage() {
    const { userId } = await auth();
    if (userId) redirect("/dashboard");

    return (
        <main className="mx-auto flex min-h-screen max-w-5xl flex-col items-center justify-center gap-6 px-6 text-center">
            <LindaWordmark className="h-14 w-auto" />
            <p className="max-w-xl text-text-muted">
                Linda listens to every call so you don&apos;t have to. Start a
                14-day sandbox with your own data, or take a guided tour of the
                public demo.
            </p>
            <div className="flex flex-wrap items-center justify-center gap-3">
                <Link href="/signup" className="btn btn-primary">
                    Start free 14-day sandbox
                </Link>
                <Link href="/sign-in" className="btn btn-ghost">
                    Sign in
                </Link>
                <a href="/" className="btn btn-ghost" target="_blank" rel="noopener">
                    See the public demo
                </a>
            </div>
        </main>
    );
}
