import { query } from '@/lib/db';
import type { Task } from '@/lib/types';
import { AgentCard } from '@/components/AgentCard';
import { WorkerToolbar } from '@/components/WorkerToolbar';
import { NightCyclePanel } from '@/components/NightCyclePanel';

export const dynamic = 'force-dynamic';

export default async function AgentsPage() {
  let runningTasks: Task[] = [];

  try {
    const result = await query<Task>(
      `SELECT t.*, r.name as repo_name FROM tasks t
       LEFT JOIN repos r ON r.id = t.repo_id
       WHERE t.status IN ('running', 'verifying')
       ORDER BY t.updated_at DESC`
    );
    runningTasks = result.rows;
  } catch (err) {
    console.error('Failed to fetch running tasks:', err);
  }

  return (
    <>
      <WorkerToolbar />
      <NightCyclePanel />

      <h2 className="mb-4">Running Agents ({runningTasks.length})</h2>
      {runningTasks.length === 0 ? (
        <p className="text-body-secondary">No agents currently running.</p>
      ) : (
        <>
          {runningTasks.map((task) => (
            <AgentCard key={task.id} task={task} />
          ))}
        </>
      )}
    </>
  );
}
