import { NextRequest, NextResponse } from 'next/server';
import { peerWorkerError, resolvePeerTask } from '@/lib/a2a/peer-task';
import { callWorker, readWorkerJson, workerUnreachableResponse } from '@/lib/worker-client';

interface RouteContext {
  params: Promise<{ id: string }>;
}

/**
 * GET /api/a2a/tasks/<id>/patches     Authorization: Bearer <peer token>
 *
 * Peer-scoped list of the `.patch` files a patch-mode task exported:
 * `{ patches: [{ name, size_bytes }] }`. This is the URL the `patches`
 * artifact hands out (lib/a2a/mapping.ts).
 */
export async function GET(request: NextRequest, context: RouteContext) {
  const { id } = await context.params;
  try {
    const resolved = await resolvePeerTask(request, id);
    if (!resolved.ok) return resolved.response;

    const workerRes = await callWorker(
      `/internal/tasks/${encodeURIComponent(resolved.taskKey)}/patches`,
    );
    if (workerRes === null) return workerUnreachableResponse();
    const body = await readWorkerJson(workerRes);
    if (!workerRes.ok) return peerWorkerError(workerRes);
    return NextResponse.json(
      { patches: Array.isArray(body.patches) ? body.patches : [] },
      { headers: { 'Cache-Control': 'no-store' } },
    );
  } catch (err) {
    console.error(`GET /api/a2a/tasks/${id}/patches error:`, err);
    return NextResponse.json({ error: 'Internal error' }, { status: 500 });
  }
}
