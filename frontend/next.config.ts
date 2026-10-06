import type { NextConfig } from "next";

const API_ORIGIN = process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000";
const isDev = process.env.NODE_ENV !== "production";

const securityHeaders = [
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  { key: "Permissions-Policy", value: "geolocation=(), microphone=(self), camera=()" },
  {
    key: "Content-Security-Policy",
    value: [
      "default-src 'self'",
        isDev
        ? "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.paddle.com"
        : "script-src 'self' https://cdn.paddle.com",
      "style-src 'self' 'unsafe-inline' https://*.paddle.com",
      "frame-src 'self' https://*.paddle.com",
      "img-src 'self' data: https:",
      "media-src 'self' https:",
      `connect-src 'self' ${API_ORIGIN} https://*.paddle.com https://api.retellai.com https://*.livekit.cloud wss://*.livekit.cloud${isDev ? " ws://localhost:*" : ""}`,
      "connect-src 'self' https://api.retellai.com https://*.livekit.cloud wss://*.livekit.cloud",
      "frame-ancestors 'none'",
      "base-uri 'self'",
      "form-action 'self'",
    ].join("; "),
  },
];

const nextConfig: NextConfig = {
  async headers() {
    return [{ source: "/:path*", headers: securityHeaders }];
  },
};

export default nextConfig;
