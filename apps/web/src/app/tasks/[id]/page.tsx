import { query } from '@/lib/db';
import { tryDbPage } from '@/lib/db-page';
import { WORKER_URL } from '@/lib/worker-url';
import { notFound } from 'next/navigation';
import type { Task, TaskRun, TaskEvent, GhostJobInfo } from '@/lib/types';
import { TaskDetail } from '@/components/TaskDetail';

export const dynamic = 'force-dynamic';

interface PageProps {
  params: Promise<{ id: string }>;
}

export default async function TaskDetailPage({ params }: PageProps) {
  const { id } = await params;
  const taskId = parseInt(id);
  if (isNaN(taskId)) notFound();

  const r = await tryDbPage(async () => {
    const taskResult = await query<Task>(
      `SELECT t.*, r.name as repo_name, r.clone_url as repo_clone_url FROM tasks t
       LEFT JOIN repos r ON r.id = t.repo_id
       WHERE t.id = $1`,
      [taskId]
    );
    if (taskResult.rows.length === 0) return null;
    const task = taskResult.rows[0];

    const runsResult = await query<TaskRun>(
      'SELECT * FROM task_runs WHERE task_id = $1 ORDER BY attempt DESC',
      [taskId]
    );
    const runs = runsResult.rows;

    const eventsResult = await query<TaskEvent>(
      'SELECT * FROM task_events WHERE task_id = $1 ORDER BY created_at DESC LIMIT 100',
      [taskId]
    );
    const events = eventsResult.rows.reverse();

    let ghost: GhostJobInfo | null = null;
    if (['running', 'verifying', 'queued'].includes(task.status) && task.queue_job_id) {
      try {
        const jobResult = await query<{ status: string }>(
          `SELECT status::text FROM pgqueuer WHERE id = $1`,
          [parseInt(task.queue_job_id)],
        );
        const queue_active = jobResult.rows.length > 0 && jobResult.rows[0].status === 'picked';

        let worker_knows = false;
        try {
          const workerRes = await fetch(`${WORKER_URL}/internal/status`, {
            signal: AbortSignal.timeout(2000),
          });
          if (workerRes.ok) {
            const workerStatus = await workerRes.json();
            worker_knows = (workerStatus.active_tasks || []).some(
              (t: { id: number }) => t.id === task.id,
            );
          }
        } catch { /* worker offline */ }

        const lockResult = task.repo_name
          ? await query<{ task_key: string }>(
              'SELECT task_key FROM repo_locks WHERE repo_name = $1',
              [task.repo_name],
            )
          : { rows: [] };

        ghost = {
          detected: queue_active && !worker_knows,
          queue_active,
          worker_knows,
          lock_held: lockResult.rows.length > 0,
        };
      } catch { /* ignore detection errors */ }
    }

    return { task, runs, events, ghost };
  });

  if (!r.ok) return r.panel;
  if (r.data === null) notFound();

  const { task, runs, events, ghost } = r.data;
  return <TaskDetail task={task} runs={runs} events={events} ghost={ghost} />;
}
