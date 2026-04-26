/** @type {import('next').NextConfig} */
const nextConfig = {
    reactStrictMode: true,
    // Ship a self-contained server bundle so the production Docker
    // image only needs the standalone output + .next/static, not the
    // full node_modules tree. Cuts the runtime image significantly.
    output: "standalone",
    async rewrites() {
        // Proxy the SPA's /api/* calls to the FastAPI backend.
        const backend = process.env.LINDA_BACKEND_URL || "http://localhost:8000";
        return [
            { source: "/api/:path*", destination: `${backend}/api/:path*` },
        ];
    },
};

export default nextConfig;
