import { NextRequest, NextResponse } from 'next/server';
import { exec } from 'child_process';
import { promisify } from 'util';
import path from 'path';
import { existsSync } from 'fs';
import { query } from '@/lib/db';

const execAsync = promisify(exec);

import { WORKER_URL } from '@/lib/worker-url';

// Resolve the worker directory from env first, then fall back to a path
// derived from DEVSERVER_ROOT, then to a path relative to the Next.js cwd
// (which is apps/web/ in dev). Hardcoding an absolute path here previously
// caused Force Reopen to silently kill the worker without restarting it on
// every host except the original prod box.
const WORKER_DIR =
  process.env.WORKER_DIR ||
  (process.env.DEVSERVER_ROOT
    ? path.join(process.env.DEVSERVER_ROOT, 'apps', 'worker')
    : path.resolve(process.cwd(), '..', 'worker'));
const WORKER_LOG = process.env.WORKER_LOG || '/tmp/worker.log';
const WORKER_UVICORN = path.join(WORKER_DIR, '.venv', 'bin', 'uvicorn');

async function isWorkerRunning(): Promise<boolean> {
  try {
    const res = await fetch(`${WORKER_URL}/health`, { signal: AbortSignal.timeout(2000) });
    return res.ok;
  } catch {
    return false;
  }
}

/**
 * Clean up stale queue state left by a crashed/killed worker:
 * - Reset any tasks stuck in running/verifying/queued back to 'pending'
 * - Cancel any picked pgqueuer jobs
 * - Release all repo locks
 */
async function cleanupStaleQueue(): Promise<{ resetTasks: number; locksReleased: number }> {
  // Reset stuck tasks
  const resetResult = await query(
    `UPDATE tasks SET status = 'pending', queue_job_id = NULL, updated_at = NOW()
     WHERE status IN ('running', 'verifying', 'queued')
     RETURNING id`,
  );
  const resetTasks = resetResult.rows.length;

  // Cancel any picked (in-progress) pgqueuer jobs that are now orphaned
  await query(
    `UPDATE pgqueuer SET status = 'canceled', updated = NOW()
     WHERE entrypoint = 'devserver-tasks' AND status IN ('queued', 'picked')`,
  );

  // Release all repo locks
  const lockResult = await query('DELETE FROM repo_locks RETURNING repo_name');
  const locksReleased = lockResult.rows.length;

  return { resetTasks, locksReleased };
}

async function getWorkerPid(): Promise<string | null> {
  try {
    const { stdout } = await execAsync(`pgrep -f "uvicorn src.main" | head -n 1`);
    const pid = stdout.trim();
    return pid || null;
  } catch {
    return null;
  }
}

export async function GET() {
  try {
    const running = await isWorkerRunning();

    if (!running) {
      return NextResponse.json({ running: false, status: null, pid: null });
    }

    const [res, pid] = await Promise.all([
      fetch(`${WORKER_URL}/internal/status`, { signal: AbortSignal.timeout(5000) }),
      getWorkerPid(),
    ]);
    const status = await res.json();
    return NextResponse.json({ running: true, status, pid });
  } catch (err) {
    console.error('GET /api/worker error:', err);
    return NextResponse.json({ running: false, status: null, pid: null });
  }
}

export async function POST(request: NextRequest) {
  const { action } = await request.json();

  if (action === 'restart' || action === 'start') {
    try {
      // Preflight: refuse to kill the running worker if we can't start the new
      // one. Force Reopen used to leave the system in a half-broken state when
      // WORKER_DIR pointed to a path that didn't exist on this host.
      if (!existsSync(WORKER_DIR)) {
        return NextResponse.json(
          { success: false, error: `WORKER_DIR does not exist: ${WORKER_DIR}. Set WORKER_DIR or DEVSERVER_ROOT in env.` },
          { status: 500 },
        );
      }
      if (!existsSync(WORKER_UVICORN)) {
        return NextResponse.json(
          { success: false, error: `uvicorn not found at ${WORKER_UVICORN}. Run \`uv sync\` in apps/worker.` },
          { status: 500 },
        );
      }

      // 1. Clean up stale queue jobs and locks before killing the old process
      const cleanup = await cleanupStaleQueue();
      console.log('Queue cleanup:', cleanup);

      // 2. Kill existing worker
      await execAsync(`pkill -f "uvicorn src.main" 2>/dev/null; sleep 1; true`).catch(() => {});

      // 3. Start new worker — nohup + stdin from /dev/null so it survives the
      //    Next.js parent (HMR reload, dev-server restart, etc.).
      const cmd = `cd ${WORKER_DIR} && find src -name "*.pyc" -delete 2>/dev/null; nohup env PYTHONPATH=src ${WORKER_UVICORN} src.main:app --host 0.0.0.0 --port 8000 < /dev/null >> ${WORKER_LOG} 2>&1 & echo $!`;
      const { stdout } = await execAsync(cmd);
      const pid = stdout.trim();

      // 4. Wait briefly then verify it came up
      await new Promise((r) => setTimeout(r, 2500));
      const running = await isWorkerRunning();

      return NextResponse.json({ success: true, pid, running, cleanup });
    } catch (err) {
      console.error('Worker restart error:', err);
      return NextResponse.json({ success: false, error: String(err) }, { status: 500 });
    }
  }

  if (action === 'stop') {
    try {
      const cleanup = await cleanupStaleQueue();
      await execAsync(`pkill -f "uvicorn src.main" 2>/dev/null; true`);
      return NextResponse.json({ success: true, cleanup });
    } catch (err) {
      return NextResponse.json({ success: false, error: String(err) }, { status: 500 });
    }
  }

  return NextResponse.json({ error: 'Unknown action' }, { status: 400 });
}
