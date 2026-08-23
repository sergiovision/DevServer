/**
 * Client-side license status cache.
 *
 * Every shell mount (AppShell footer badge) and the Settings LicensePanel
 * used to fire their own `/api/pro/license` roundtrip (browser → Next.js →
 * worker → DB). License state changes at most a few times a day, so a short
 * sessionStorage cache + in-flight deduplication makes the check effectively
 * free on reloads and navigations.
 *
 * Free build: the `/api/pro/license` route is stripped (404) — the helper
 * resolves to null and caches nothing, exactly like the old inline fetch.
 */

export interface LicenseStatus {
  kind: string; // 'trial' | 'licensed' | 'free' | 'expired'
  plan: string; // 'pro' | 'free'
  licenseKey?: string;
  expiresAt?: string | null;
  trialDaysLeft?: number | null;
  isActive: boolean;
  message?: string;
  /** capitaltools storefront page for this product, built by the worker from
   *  CAPITALTOOLS_URL so a self-hosted issuer points at its own store. */
  buyUrl?: string;
}

const CACHE_KEY = 'devserver.license.v1';
const TTL_MS = 5 * 60 * 1000;

let inflight: Promise<LicenseStatus | null> | null = null;

/** Synchronous cache read — null when absent, expired, or not in a browser. */
export function readLicenseCache(): LicenseStatus | null {
  if (typeof window === 'undefined') return null;
  try {
    const raw = sessionStorage.getItem(CACHE_KEY);
    if (!raw) return null;
    const { at, data } = JSON.parse(raw);
    if (typeof at !== 'number' || Date.now() - at > TTL_MS) return null;
    return data ?? null;
  } catch {
    return null;
  }
}

/** Overwrite the cache — call with the state returned by a license action. */
export function primeLicenseCache(data: LicenseStatus): void {
  try {
    sessionStorage.setItem(CACHE_KEY, JSON.stringify({ at: Date.now(), data }));
  } catch {
    /* storage unavailable — caching is best-effort */
  }
}

/**
 * Resolve the license status: cached value when fresh, otherwise one shared
 * network fetch (concurrent callers await the same promise). Pass
 * `{ fresh: true }` to bypass the cache (still deduped against an in-flight
 * request).
 */
export function getLicenseStatus(opts?: { fresh?: boolean }): Promise<LicenseStatus | null> {
  if (!opts?.fresh) {
    const cached = readLicenseCache();
    if (cached) return Promise.resolve(cached);
  }
  if (!inflight) {
    inflight = fetch('/api/pro/license')
      .then((res) => (res.ok ? res.json() : null))
      .then((data: LicenseStatus | null) => {
        if (data && typeof data.plan === 'string' && data.plan) primeLicenseCache(data);
        return data;
      })
      .catch(() => null)
      .finally(() => {
        inflight = null;
      });
  }
  return inflight;
}
