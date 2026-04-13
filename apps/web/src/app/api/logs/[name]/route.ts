import { NextRequest, NextResponse } from 'next/server';
import { promises as fs } from 'fs';
import path from 'path';
import os from 'os';

export const dynamic = 'force-dynamic';

const ALLOWED: Record<string, string> = {
  worker: 'worker.log',
  web: 'web.log',
};

const MAX_INITIAL_BYTES = 256 * 1024; // tail last 256KB on first load
const MAX_APPEND_BYTES = 1024 * 1024; // cap per-poll append

function projectRoot(): string {
  if (process.env.DEVSERVER_ROOT) return process.env.DEVSERVER_ROOT;
  // apps/web/.next or apps/web — walk up to repo root
  return path.resolve(process.cwd(), '..', '..');
}

async function resolveLogPath(name: string): Promise<string | null> {
  const filename = ALLOWED[name];
  if (!filename) return null;

  const envKey = `${name.toUpperCase()}_LOG`; // WORKER_LOG, WEB_LOG
  const candidates: string[] = [];

  if (process.env[envKey]) candidates.push(process.env[envKey] as string);
  if (process.env.DS_LOG_DIR) candidates.push(path.join(process.env.DS_LOG_DIR, filename));
  candidates.push(path.join(projectRoot(), 'logs', filename));
  candidates.push(path.join(os.tmpdir(), filename));
  candidates.push(path.join('/tmp', filename));

  for (const p of candidates) {
    try {
      await fs.access(p);
      return p;
    } catch {
      // keep looking
    }
  }
  // Return last candidate anyway so caller can surface a helpful error
  return candidates[candidates.length - 1] ?? null;
}

export async function GET(
  req: NextRequest,
  { params }: { params: Promise<{ name: string }> },
) {
  const { name } = await params;
  if (!(name in ALLOWED)) {
    return NextResponse.json({ error: 'Unknown log' }, { status: 400 });
  }

  const sinceParam = req.nextUrl.searchParams.get('since');
  const since = Math.max(0, Number.parseInt(sinceParam ?? '0', 10) || 0);

  const filePath = await resolveLogPath(name);
  if (!filePath) {
    return NextResponse.json({ lines: [], nextOffset: 0 });
  }

  let size = 0;
  try {
    const stat = await fs.stat(filePath);
    size = stat.size;
  } catch {
    return NextResponse.json({ lines: [], nextOffset: 0 });
  }

  // File rotated/truncated: restart from 0
  let start = since;
  if (since > size) start = 0;

  // First load (since=0): tail the end of the file
  if (start === 0 && size > MAX_INITIAL_BYTES) {
    start = size - MAX_INITIAL_BYTES;
  }

  let end = size;
  if (end - start > MAX_APPEND_BYTES) {
    start = end - MAX_APPEND_BYTES;
  }

  if (end <= start) {
    return NextResponse.json({ lines: [], nextOffset: size });
  }

  let handle;
  try {
    handle = await fs.open(filePath, 'r');
    const length = end - start;
    const buf = Buffer.alloc(length);
    await handle.read(buf, 0, length, start);
    let text = buf.toString('utf8');

    // If we cut into the middle of a line, drop the partial leading fragment
    // (unless we're starting from byte 0).
    if (start > 0) {
      const nl = text.indexOf('\n');
      if (nl >= 0) {
        text = text.slice(nl + 1);
      }
    }

    // Drop trailing partial line; keep its bytes for the next poll
    let consumedEnd = end;
    const lastNl = text.lastIndexOf('\n');
    if (lastNl >= 0 && lastNl < text.length - 1) {
      const trailing = text.length - 1 - lastNl;
      consumedEnd = end - trailing;
      text = text.slice(0, lastNl + 1);
    } else if (lastNl < 0) {
      // No newline at all in our window — defer everything to next poll
      return NextResponse.json({ lines: [], nextOffset: start });
    }

    const lines = text.length > 0
      ? text.replace(/\n$/, '').split('\n')
      : [];

    return NextResponse.json({ lines, nextOffset: consumedEnd });
  } catch (err) {
    return NextResponse.json(
      { lines: [], nextOffset: since, error: String(err) },
      { status: 200 },
    );
  } finally {
    if (handle) await handle.close().catch(() => {});
  }
}
