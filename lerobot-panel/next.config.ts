import type { NextConfig } from "next";
import { PHASE_DEVELOPMENT_SERVER } from "next/constants";

// The UI is exported as static files and served by the FastAPI backend (server/main.py).
// During `npm run dev`, API calls are proxied to that backend instead.
const API_ORIGIN = process.env.PANEL_API_ORIGIN ?? "http://127.0.0.1:8000";

export default function config(phase: string): NextConfig {
  const base: NextConfig = {
    turbopack: {
      rules: {
        "*.css": {
          loaders: ["@tailwindcss/turbopack"],
          as: "*.css",
        },
      },
    },
  };
  if (phase === PHASE_DEVELOPMENT_SERVER) {
    return {
      ...base,
      async rewrites() {
        return [{ source: "/api/:path*", destination: `${API_ORIGIN}/api/:path*` }];
      },
    };
  }
  return { ...base, output: "export", images: { unoptimized: true } };
}
