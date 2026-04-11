/**
 * GET /api/task-patches/[key]/file/[filename]
 *
 * Streams a single patch (or the combined mbox) back to the browser as a
 * file download. Proxies to the worker's
 * /internal/tasks/{task_key}/patches/file/{filename} endpoint.
 *
 * The worker is the authority on filename validation — this route only
 * forwards what it receives. A bad filename on the worker side returns
 * 404, which we bubble up unchanged.
 */
import { NextRequest, NextResponse } from 'next/server';
import { WORKER_URL } from '@/lib/worker-url';

interface RouteContext {
  params: Promise<{ key: string; filename: string }>;
}

export async function GET(_req: NextRequest, { params }: RouteContext) {
  const { key, filename } = await params;

  try {
    const res = await fetch(
      `${WORKER_URL}/internal/tasks/${encodeURIComponent(key)}/patches/file/${encodeURIComponent(filename)}`,
      { cache: 'no-store' },
    );

    if (!res.ok || !res.body) {
      return NextResponse.json(
        { error: `worker returned ${res.status}` },
        { status: res.status },
      );
    }

    // Stream the response body through as a download. We replace the
    // content-disposition the worker sets so that the browser always
    // saves-as with the expected filename.
    const contentType = res.headers.get('content-type') ?? 'application/octet-stream';
    const headers = new Headers({
      'Content-Type': contentType,
      'Content-Disposition': `attachment; filename="${filename}"`,
      'Cache-Control': 'no-store',
    });
    const contentLength = res.headers.get('content-length');
    if (contentLength) headers.set('Content-Length', contentLength);

    return new NextResponse(res.body, { status: 200, headers });
  } catch {
    return NextResponse.json(
      { error: 'worker unreachable' },
      { status: 502 },
    );
  }
}
