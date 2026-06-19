import { NextRequest, NextResponse } from 'next/server';
import { apiErrorResponse } from '@/lib/api-errors';
import { WORKER_URL } from '@/lib/worker-url';

interface RouteContext {
  params: Promise<{ id: string }>;
}

/**
 * POST /api/repos/<id>/reindex
 *
 * Reindex this repository's memory by reusing the same worker corpus-ingest
 * endpoint the devserver-memory MCP `corpus_ingest` tool calls. Both the
 * `code` and `doc` corpora are kicked off so the whole repo memory is
 * rebuilt. The worker runs each ingest in the background and returns
 * immediately, so success here means "reindex started".
 */
export async function POST(_request: NextRequest, context: RouteContext) {
  const { id } = await context.params;
  const repoId = parseInt(id);

  try {
    const results = await Promise.all(
      (['code', 'doc'] as const).map(async (kind) => {
        const res = await fetch(`${WORKER_URL}/internal/repos/${repoId}/corpus/ingest`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ kind, full: false }),
        });
        const data = await res.json().catch(() => ({}));
        return { ok: res.ok, status: res.status, kind, data };
      }),
    );

    const failed = results.find((r) => !r.ok);
    if (failed) {
      const detail =
        (failed.data as { detail?: string; error?: string }).detail ||
        (failed.data as { detail?: string; error?: string }).error ||
        'Reindex failed';
      return NextResponse.json({ error: detail }, { status: failed.status });
    }

    return NextResponse.json({ ok: true, results });
  } catch (err) {
    return apiErrorResponse(err, `POST /api/repos/${id}/reindex`);
  }
}
