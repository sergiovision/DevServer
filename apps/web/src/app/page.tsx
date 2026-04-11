import { query } from '@/lib/db';
import { Dashboard } from '@/components/Dashboard';
import type { Task, DailyStats } from '@/lib/types';

export const dynamic = 'force-dynamic';

export default async function DashboardPage() {
  let runningTasks: Task[] = [];
  let queuedTasks: Task[] = [];
  let todayStats = { completed: 0, failed: 0, cost_usd: 0 };

  try {
    const runningResult = await query<Task>(
      `SELECT t.*, r.name as repo_name FROM tasks t
       LEFT JOIN repos r ON r.id = t.repo_id
       WHERE t.status = 'running'
       ORDER BY t.updated_at DESC`
    );
    runningTasks = runningResult.rows;

    const queuedResult = await query<Task>(
      `SELECT t.*, r.name as repo_name FROM tasks t
       LEFT JOIN repos r ON r.id = t.repo_id
       WHERE t.status IN ('queued', 'pending')
       ORDER BY t.priority ASC, t.created_at ASC
       LIMIT 20`
    );
    queuedTasks = queuedResult.rows;

    const statsResult = await query<DailyStats>(
      `SELECT completed, failed, cost_usd FROM daily_stats WHERE date = CURRENT_DATE`
    );
    if (statsResult.rows.length > 0) {
      todayStats = statsResult.rows[0];
    }
  } catch (err) {
    console.error('Dashboard data fetch error:', err);
  }

  return (
    <Dashboard
      runningTasks={runningTasks}
      queuedTasks={queuedTasks}
      todayStats={todayStats}
    />
  );
}
