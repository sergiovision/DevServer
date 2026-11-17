import { NextRequest, NextResponse } from 'next/server';
import {
  callWorker,
  readWorkerJson,
  workerErrorResponse,
  workerUnreachableResponse,
} from '@/lib/worker-client';

/**
 * GET /api/night-cycle — current night-cycle state from the worker.
 * A worker that cannot be reached reports `{active: false}` so the dashboard
 * panel simply shows "Off".
 */
export async function GET() {
  const res = await callWorker('/internal/night-cycle/status', {}, 3000);
  if (res === null) return NextResponse.json({ active: false });
  const body = await readWorkerJson(res);
  if (!res.ok) return workerErrorResponse(res, body);
  return NextResponse.json(body);
}

/**
 * POST /api/night-cycle
 *
 * Body: { action: 'start' | 'stop', end_hour?: number (UTC, default 7),
 *         restart_on_failed?: boolean (default false) }
 * Any other action is a 400.
 */
export async function POST(request: NextRequest) {
  const raw: unknown = await request.json().catch(() => null);
  const body = raw && typeof raw === 'object' ? (raw as Record<string, unknown>) : {};
  if (body.action !== 'start' && body.action !== 'stop') {
    return NextResponse.json(
      { error: "action must be 'start' or 'stop'" },
      { status: 400 },
    );
  }
  const endpoint = body.action;
  const endHour = typeof body.end_hour === 'number' ? body.end_hour : 7;
  const restartOnFailed = body.restart_on_failed === true;

  const res = await callWorker(
    `/internal/night-cycle/${endpoint}`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(
        endpoint === 'start'
          ? { end_hour: endHour, restart_on_failed: restartOnFailed }
          : {},
      ),
    },
    10_000,
  );
  if (res === null) return workerUnreachableResponse();
  const data = await readWorkerJson(res);
  if (!res.ok) return workerErrorResponse(res, data);
  return NextResponse.json(data);
}
