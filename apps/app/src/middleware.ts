import { clerkMiddleware, createRouteMatcher } from "@clerk/nextjs/server";

// Routes anonymous visitors can reach. API routes are always passed
// through so the FastAPI backend (which has its own Clerk + API-key
// auth) can return proper JSON 401/402 responses instead of Clerk's
// redirect-to-sign-in behaviour.
const isPublic = createRouteMatcher([
    "/",
    "/sign-in(.*)",
    "/sign-up(.*)",
    "/signup",
    "/api/(.*)",
]);

export default clerkMiddleware(async (auth, req) => {
    if (!isPublic(req)) {
        await auth.protect();
    }
});

export const config = {
    matcher: [
        // Skip Next internals and static files
        "/((?!_next|[^?]*\\.(?:html?|css|js|json|woff2?|ttf|otf|eot|png|jpe?g|gif|webp|svg|ico|map)).*)",
        "/(api|trpc)(.*)",
    ],
};
