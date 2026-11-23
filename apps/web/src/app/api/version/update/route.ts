/**
 * GET /api/version/update — release status for the footer notice.
 *
 * Relays the worker's cached GitHub release check
 * (`/internal/update/status`) and adds the operator's per-release dismissal
 * (`update_dismissed_build` setting).
 *
 * Invariant: always answers 200. The notice is an optional nicety; a down
 * worker or database must not raise an error toast for it, so every failure
 * degrades to `status: 'unknown'` (which shows nothing). The connectivity
 * banner is what reports a down worker.
 */

import { NextResponse } from 'next/server';
import { query } from '@/lib/db';
import { callWorker, readWorkerJson } from '@/lib/route-utils';
import {
  DISMISSED_SETTING_KEY,
  isReleaseVersion,
  unknownUpdateStatus,
  type UpdateNoticeResponse,
  type UpdateStatusName,
  type UpdateStatusPayload,
} from '@/lib/update-notice';

export const dynamic = 'force-dynamic';

const STATUS_NAMES: ReadonlySet<string> = new Set<UpdateStatusName>([
  'up_to_date',
  'update_available',
  'unknown',
  'disabled',
]);

function stringOrNull(value: unknown): string | null {
  return typeof value === 'string' && value ? value : null;
}

async function workerStatus(currentBuild: string | undefined): Promise<UpdateStatusPayload> {
  // The worker image may not ship version.json; the web build always knows
  // its own version, so pass it along as the fallback.
  const qs = currentBuild ? `?current=${encodeURIComponent(currentBuild)}` : '';
  const res = await callWorker(`/internal/update/status${qs}`, {}, 15_000);
  if (!res || !res.ok) return unknownUpdateStatus();
  const body = await readWorkerJson(res);
  const status = typeof body.status === 'string' && STATUS_NAMES.has(body.status)
    ? (body.status as UpdateStatusName)
    : 'unknown';
  return {
    status,
    current: stringOrNull(body.current),
    latest: stringOrNull(body.latest),
    release_url: stringOrNull(body.release_url),
    checked_at: stringOrNull(body.checked_at),
  };
}

async function dismissedBuild(): Promise<string | null> {
  try {
    const result = await query<{ value: unknown }>('SELECT value FROM settings WHERE key = $1', [
      DISMISSED_SETTING_KEY,
    ]);
    const value = result.rows[0]?.value;
    return isReleaseVersion(value) ? value : null;
  } catch {
    return null;
  }
}

export async function GET() {
  const [status, dismissed] = await Promise.all([
    workerStatus(process.env.NEXT_PUBLIC_VERSION),
    dismissedBuild(),
  ]);
  const body: UpdateNoticeResponse = { ...status, dismissed_build: dismissed };
  return NextResponse.json(body, { headers: { 'Cache-Control': 'no-store' } });
}
