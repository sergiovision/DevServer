import { query } from '@/lib/db';
import { tryDbPage } from '@/lib/db-page';
import { Dashboard } from '@/components/Dashboard';
import type { Task, DailyStats } from '@/lib/types';

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

    const statsResult = await query<DailyStats>(
      `SELECT completed, failed, cost_usd FROM daily_stats WHERE date = CURRENT_DATE`
    );

    return {
      runningTasks: runningResult.rows,
      queuedTasks: queuedResult.rows,
      todayStats: statsResult.rows[0] ?? { completed: 0, failed: 0, cost_usd: 0 },
    };
  });

  if (!r.ok) return r.panel;
  const { runningTasks, queuedTasks, todayStats } = r.data;

  return (
    <Dashboard
      runningTasks={runningTasks}
      queuedTasks={queuedTasks}
      todayStats={todayStats}
    />
  );
}
