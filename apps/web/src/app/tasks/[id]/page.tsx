import { query } from '@/lib/db';
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

  let task: Task | null = null;
  let runs: TaskRun[] = [];
  let events: TaskEvent[] = [];
  let ghost: GhostJobInfo | null = null;

  try {
    const taskResult = await query<Task>(
      `SELECT t.*, r.name as repo_name, r.clone_url as repo_clone_url FROM tasks t
       LEFT JOIN repos r ON r.id = t.repo_id
       WHERE t.id = $1`,
      [taskId]
    );
    if (taskResult.rows.length === 0) notFound();
    task = taskResult.rows[0];

    const runsResult = await query<TaskRun>(
      'SELECT * FROM task_runs WHERE task_id = $1 ORDER BY attempt DESC',
      [taskId]
    );
    runs = runsResult.rows;

    const eventsResult = await query<TaskEvent>(
      'SELECT * FROM task_events WHERE task_id = $1 ORDER BY created_at DESC LIMIT 100',
      [taskId]
    );
    events = eventsResult.rows.reverse();

    // Ghost job detection: task marked running/verifying but worker isn't processing it
    if (['running', 'verifying', 'queued'].includes(task!.status) && task!.queue_job_id) {
      try {
        // Check if the pgqueuer job is still active (picked)
        const jobResult = await query<{ status: string }>(
          `SELECT status::text FROM pgqueuer WHERE id = $1`,
          [parseInt(task!.queue_job_id)],
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
              (t: { id: number }) => t.id === task!.id,
            );
          }
        } catch { /* worker offline */ }

        // Check repo lock
        const lockResult = task!.repo_name
          ? await query<{ task_key: string }>(
              'SELECT task_key FROM repo_locks WHERE repo_name = $1',
              [task!.repo_name],
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
  } catch (err) {
    // Re-throw Next.js notFound() so it renders the 404 page instead of an error
    if ((err as { digest?: string })?.digest === 'NEXT_NOT_FOUND') throw err;
    console.error('Failed to fetch task detail:', err);
    notFound();
  }

  return <TaskDetail task={task!} runs={runs} events={events} ghost={ghost} />;
}
