import { query } from '@/lib/db';
import { tryDbPage } from '@/lib/db-page';
import { Dashboard } from '@/components/Dashboard';
import type { Task } from '@/lib/types';

export const dynamic = 'force-dynamic';

export default async function DashboardPage() {
  const r = await tryDbPage(async () => {
    const runningResult = await query<Task>(
      `SELECT t.*, r.name as repo_name FROM tasks t
       LEFT JOIN repos r ON r.id = t.repo_id
       WHERE t.status IN ('running', 'verifying')
       ORDER BY t.updated_at DESC`
    );

    const queuedResult = await query<Task>(
      `SELECT t.*, r.name as repo_name FROM tasks t
       LEFT JOIN repos r ON r.id = t.repo_id
       WHERE t.status IN ('queued', 'pending')
       ORDER BY t.priority ASC, t.created_at ASC
       LIMIT 20`
    );

    return {
      runningTasks: runningResult.rows,
      queuedTasks: queuedResult.rows,
    };
  });

  if (!r.ok) return r.panel;
  const { runningTasks, queuedTasks } = r.data;

  return (
    <Dashboard
      runningTasks={runningTasks}
      queuedTasks={queuedTasks}
    />
  );
}
