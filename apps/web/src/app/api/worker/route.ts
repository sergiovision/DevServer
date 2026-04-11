import { NextRequest, NextResponse } from 'next/server';
import { exec } from 'child_process';
import { promisify } from 'util';
import { query } from '@/lib/db';

const execAsync = promisify(exec);

import { WORKER_URL } from '@/lib/worker-url';
const WORKER_DIR = '/home/serg/devserver/apps/worker';
const WORKER_LOG = '/tmp/worker.log';

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

export async function GET() {
  try {
    const running = await isWorkerRunning();

    if (!running) {
      return NextResponse.json({ running: false, status: null });
    }

    const res = await fetch(`${WORKER_URL}/internal/status`, {
      signal: AbortSignal.timeout(5000),
    });
    const status = await res.json();
    return NextResponse.json({ running: true, status });
  } catch (err) {
    console.error('GET /api/worker error:', err);
    return NextResponse.json({ running: false, status: null });
  }
}

export async function POST(request: NextRequest) {
  const { action } = await request.json();

  if (action === 'restart' || action === 'start') {
    try {
      // 1. Clean up stale queue jobs and locks before killing the old process
      const cleanup = await cleanupStaleQueue();
      console.log('Queue cleanup:', cleanup);

      // 2. Kill existing worker
      await execAsync(`pkill -f "uvicorn src.main" 2>/dev/null; sleep 1; true`).catch(() => {});

      // 3. Start new worker
      const cmd = `cd ${WORKER_DIR} && find src -name "*.pyc" -delete 2>/dev/null; PYTHONPATH=src .venv/bin/uvicorn src.main:app --host 0.0.0.0 --port 8000 >> ${WORKER_LOG} 2>&1 & echo $!`;
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
