/**
 * GET /api/health/connectivity — the connectivity banner's probe.
 *
 * Why it exists: `/api/health` runs every dependency check (queue,
 * filesystem, notifications) with 5 s timeouts and answers 503 when
 * unhealthy — too slow and too noisy to poll every few seconds from every
 * open tab. The banner only needs three facts, fast:
 *
 *   200 {server: true, worker: boolean, db: boolean}
 *
 * Invariants:
 * - Always answers 200 when this server is running: any status other than 200
 *   (or no answer) is how the browser learns the *server* is down, so the
 *   route never reports its own dependencies' failures as an HTTP error.
 * - Each dependency check is capped at `CHECK_TIMEOUT_MS` so the probe stays
 *   well under the banner's 3 s degraded poll interval.
 */

import { NextResponse } from 'next/server';
import { checkDatabase } from '@/lib/health-checks';
import { callWorker } from '@/lib/route-utils';

export const dynamic = 'force-dynamic';

const CHECK_TIMEOUT_MS = 2000;

/** Resolve to `fallback` if `work` has not settled in time; never rejects. */
async function within<T>(work: Promise<T>, timeoutMs: number, fallback: T): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<T>((resolve) => {
    timer = setTimeout(() => resolve(fallback), timeoutMs);
  });
  try {
    return await Promise.race([work.catch(() => fallback), timeout]);
  } finally {
    clearTimeout(timer);
  }
}

async function workerReachable(): Promise<boolean> {
  const res = await callWorker('/health', {}, CHECK_TIMEOUT_MS);
  return res?.ok ?? false;
}

async function databaseReachable(): Promise<boolean> {
  const check = await checkDatabase();
  return check.status !== 'unhealthy';
}

export async function GET() {
  const [worker, db] = await Promise.all([
    within(workerReachable(), CHECK_TIMEOUT_MS + 250, false),
    within(databaseReachable(), CHECK_TIMEOUT_MS, false),
  ]);
  return NextResponse.json(
    { server: true, worker, db },
    { status: 200, headers: { 'Cache-Control': 'no-store' } },
  );
}
