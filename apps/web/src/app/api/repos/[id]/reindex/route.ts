import { NextRequest, NextResponse } from 'next/server';
import {
  callWorker,
  invalidId,
  parseRouteId,
  readWorkerJson,
  workerError,
  workerUnreachable,
  type RouteContext,
} from '@/lib/route-utils';

/**
 * POST /api/repos/<id>/reindex
 *
 * Reindex this repository's memory by reusing the same worker corpus-ingest
 * endpoint the DevServer MCP `corpus_ingest` tool calls. Both the
 * `code` and `doc` corpora are kicked off so the whole repo memory is
 * rebuilt. The worker runs each ingest in the background and returns
 * immediately, so success here means "reindex started".
 */
export async function POST(_request: NextRequest, context: RouteContext) {
  const { id } = await context.params;
  const repoId = parseRouteId(id);
  if (repoId === null) return invalidId('repo id');

  const calls = await Promise.all(
    (['code', 'doc'] as const).map(async (kind) => {
      const res = await callWorker(`/internal/repos/${repoId}/corpus/ingest`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ kind, full: false }),
      });
      const data: Record<string, unknown> = res ? await readWorkerJson(res) : {};
      return { res, kind, data };
    }),
  );

  if (calls.some((call) => call.res === null)) return workerUnreachable();
  const failed = calls.find((call) => call.res !== null && !call.res.ok);
  if (failed?.res) return workerError(failed.res, failed.data);

  const results = calls.map((call) => ({
    ok: call.res?.ok ?? false,
    status: call.res?.status ?? 0,
    kind: call.kind,
    data: call.data,
  }));
  return NextResponse.json({ ok: true, results });
}
