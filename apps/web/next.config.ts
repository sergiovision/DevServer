import type { NextConfig } from 'next';
import { readFileSync, existsSync } from 'fs';
import { resolve } from 'path';
import { hostname, userInfo } from 'os';

// Read version from project root
let versionString = '0.0.0';
try {
  const versionPath = resolve(__dirname, '..', '..', 'version.json');
  const v = JSON.parse(readFileSync(versionPath, 'utf-8'));
  versionString = `${v.major}.${v.minor}.${v.build}`;
} catch { /* fallback */ }

// Determine edition: 'pro' (full version) or 'free'. The free version is
// produced by `scripts/strip-pro.sh`, which deletes every `pro/` folder.
// We detect it by probing for the pro folders that script removes — if the
// pro components/services are gone, this is a free build.
const edition = (
  existsSync(resolve(__dirname, 'src', 'components', 'pro')) ||
  existsSync(resolve(__dirname, '..', 'worker', 'src', 'services', 'pro'))
) ? 'pro' : 'free';

// Determine deploy mode: docker | production | development
const deployMode = process.env.DEPLOY_MODE
  || (process.env.NODE_ENV === 'production' ? 'production' : 'development');

const nextConfig: NextConfig = {
  reactStrictMode: true,
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
    NEXT_PUBLIC_EDITION: edition,
    NEXT_PUBLIC_HOSTNAME: hostname(),
    NEXT_PUBLIC_USER: userInfo().username,
  },
  // A2A agent discovery (Pro). The protocol mandates the RFC 8615 well-known
  // path, but the handler lives under app/api/pro/ so `strip-pro.sh` removes it
  // with the rest of the Pro bundle — hence a rewrite rather than a real route.
  // Gated on `edition` so a free build 404s here instead of advertising a
  // gateway whose endpoint no longer exists.
  async rewrites() {
    if (edition !== 'pro') return [];
    return [
      {
        source: '/.well-known/agent-card.json',
        destination: '/api/pro/a2a/card',
      },
    ];
  },
};

export default nextConfig;
