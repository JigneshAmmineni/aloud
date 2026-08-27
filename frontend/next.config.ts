import type { NextConfig } from "next";

// Signaling requests proxy to the backend (same-origin from the browser's
// point of view, so no CORS). Media itself flows browser <-> backend directly
// over WebRTC; only the HTTP signaling passes through here.
const BACKEND = process.env.BACKEND_URL ?? "http://localhost:7860";

const nextConfig: NextConfig = {
  async rewrites() {
    return [
      { source: "/api/offer", destination: `${BACKEND}/api/offer` },
      { source: "/api/admin/:path*", destination: `${BACKEND}/api/admin/:path*` },
      { source: "/start", destination: `${BACKEND}/start` },
      { source: "/documents", destination: `${BACKEND}/documents` },
      { source: "/sessions/:path*", destination: `${BACKEND}/sessions/:path*` },
    ];
  },
};

export default nextConfig;
