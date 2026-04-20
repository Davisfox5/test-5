/** @type {import('next').NextConfig} */
const nextConfig = {
    reactStrictMode: true,
    async rewrites() {
        // Proxy the SPA's /api/* calls to the FastAPI backend in dev.
        // In production, nginx / ALB / Vercel Edge handles this routing.
        const backend = process.env.LINDA_BACKEND_URL || "http://localhost:8000";
        return [
            { source: "/api/:path*", destination: `${backend}/api/:path*` },
        ];
    },
};

export default nextConfig;
