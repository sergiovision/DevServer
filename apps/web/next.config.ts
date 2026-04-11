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
