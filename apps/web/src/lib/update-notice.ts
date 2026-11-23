/**
 * Pure decisions behind the footer's "new version available" notice.
 *
 * Why it exists: the notice must stay invisible in every uncertain case (no
 * network, GitHub rate limit, worker down, check disabled) and must respect a
 * per-release dismissal — dismissing 1.0.21 hides 1.0.21 only, so 1.0.22
 * shows again. Keeping that logic here, away from React, lets it be tested
 * without a DOM.
 *
 * Invariants:
 * - Only `status === 'update_available'` with a parseable `latest` can show.
 * - The dismissal key (`update_dismissed_build` setting) holds the full
 *   `major.minor.build` string of the dismissed release, not just the build
 *   number, so `1.1.0` is never confused with `1.0.0`.
 * - The suggested update command depends on how this instance runs (Docker
 *   vs host) and the operator's OS; all inputs are passed in, nothing reads
 *   globals.
 */

export type UpdateStatusName = 'up_to_date' | 'update_available' | 'unknown' | 'disabled';

/** Worker `/internal/update/status` body. */
export interface UpdateStatusPayload {
  status: UpdateStatusName;
  current: string | null;
  latest: string | null;
  release_url: string | null;
  checked_at: string | null;
}

/** `GET /api/version/update` body: the worker status plus the dismissal. */
export interface UpdateNoticeResponse extends UpdateStatusPayload {
  dismissed_build: string | null;
}

export type HostKind = 'docker' | 'windows' | 'unix';

/** Settings key holding the dismissed release (`"1.0.21"`). */
export const DISMISSED_SETTING_KEY = 'update_dismissed_build';

const RELEASE_VERSION_RE = /^\d+\.\d+\.\d+$/;

/** True for a bare `major.minor.build` — the only shape the dismiss route stores. */
export function isReleaseVersion(value: unknown): value is string {
  return typeof value === 'string' && RELEASE_VERSION_RE.test(value);
}

/** The worker's answer when it could not be reached or answered garbage. */
export function unknownUpdateStatus(current: string | null = null): UpdateStatusPayload {
  return { status: 'unknown', current, latest: null, release_url: null, checked_at: null };
}

/** Show the notice? Null/failed status, disabled, unknown, or a dismissed release → no. */
export function shouldShowUpdateNotice(
  response: Pick<UpdateNoticeResponse, 'status' | 'latest' | 'dismissed_build'> | null | undefined,
): boolean {
  if (!response) return false;
  if (response.status !== 'update_available') return false;
  if (!isReleaseVersion(response.latest)) return false;
  return response.latest !== response.dismissed_build;
}

/**
 * How this instance is deployed and what OS the operator is on.
 * `buildVersion` is `NEXT_PUBLIC_VERSION` (`1.0.20.docker`, `1.0.20.production`, …).
 */
export function detectHostKind(input: {
  buildVersion: string | undefined;
  platform?: string;
  userAgent?: string;
}): HostKind {
  if (input.buildVersion?.endsWith('.docker')) return 'docker';
  // Word boundary so "darwin" (a Node-style platform string) is not Windows.
  const fingerprint = `${input.platform ?? ''} ${input.userAgent ?? ''}`;
  return /\bwin/i.test(fingerprint) ? 'windows' : 'unix';
}

/** The command an operator runs from the repo root to update. */
export function updateCommand(host: HostKind): string {
  switch (host) {
    case 'docker':
      return 'git pull && ./scripts/start.sh --docker';
    case 'windows':
      return 'git pull; ./scripts/restart.ps1';
    default:
      return 'git pull && ./scripts/restart.sh --prod';
  }
}
