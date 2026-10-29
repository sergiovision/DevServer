import type { NextConfig } from 'next';
import { readFileSync } from 'fs';
import { resolve } from 'path';
import { hostname, userInfo } from 'os';

// Read version from project root
let versionString = '0.0.0';
try {
  const versionPath = resolve(__dirname, '..', '..', 'version.json');
  const v = JSON.parse(readFileSync(versionPath, 'utf-8'));
  versionString = `${v.major}.${v.minor}.${v.build}`;
} catch { /* fallback */ }

// Determine deploy mode: docker | production | development
const deployMode = process.env.DEPLOY_MODE
  || (process.env.NODE_ENV === 'production' ? 'production' : 'development');

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // Emit `.next/standalone/` — the app plus only the node_modules the build
  // actually traced. The Docker runtime stage copies that instead of the full
  // dependency tree (hundreds of MB of CoreUI Pro / chart.js / mermaid source).
  //
  // We keep our own custom server (server.ts → server.js) as the entrypoint
  // rather than the `server.js` Next generates here: it terminates the
  // `/api/ws` WebSocket upgrade that carries every live task update. See the
  // `web` and `combined` targets in docker/Dockerfile for how the two are
  // combined.
  output: 'standalone',
  // Pin the tracing root to apps/web. Left to infer it, Next walks up looking
  // for a lockfile, finds the repo-root `package-lock.json`, and nests the
  // output at `.next/standalone/apps/web/` — a layout that would differ from
  // the Docker build (where apps/web IS the context root) and silently break
  // the COPY paths in docker/Dockerfile's webbuild stage.
  outputFileTracingRoot: __dirname,
  // Tracing starts from the App Router entry points, so it never sees `ws` —
  // that import lives only in the custom server, which Next does not compile.
  // Name it explicitly or `node server.js` dies on MODULE_NOT_FOUND.
  outputFileTracingIncludes: {
    '/**': ['./node_modules/ws/**'],
  },
  // Origins allowed to reach the Next.js dev server internals (server actions,
  // RSC/data requests, HMR). Local access works via 127.0.0.1 + hostname();
  // Tailscale hands out addresses from the CGNAT block 100.64.0.0/10, so match
  // the whole `100.*.*.*` range instead of pinning a single IP (which goes
  // stale whenever the Tailscale address changes). MagicDNS names are covered
  // by `*.tail*.ts.net`.
  allowedDevOrigins: [
    '127.0.0.1',
    'localhost',
    '100.124.243.58',  // current Tailscale IP (explicit belt-and-suspenders)
    '100.*.*.*',       // all Tailscale CGNAT addresses (100.64.0.0/10)
    '*.tail*.ts.net',  // Tailscale MagicDNS hostnames
    hostname(),
  ],
  transpilePackages: [
    '@coreui/coreui-pro',
    '@coreui/react-pro',
    '@coreui/icons',
    '@coreui/icons-react',
  ],
  experimental: {
    serverActions: {
      bodySizeLimit: '2mb',
    },
  },
  env: {
    NEXT_PUBLIC_VERSION: `${versionString}.${deployMode}`,
    NEXT_PUBLIC_HOSTNAME: hostname(),
    NEXT_PUBLIC_USER: userInfo().username,
  },
};

export default nextConfig;
