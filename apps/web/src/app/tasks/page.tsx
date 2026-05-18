import { query } from '@/lib/db';
import { tryDbPage } from '@/lib/db-page';
import { TasksView } from '@/components/TasksView';
import type { Task } from '@/lib/types';

export const dynamic = 'force-dynamic';

export default async function TasksPage() {
  const r = await tryDbPage(async () => {
    const result = await query<Task>(
      `SELECT t.*, r.name as repo_name FROM tasks t
       LEFT JOIN repos r ON r.id = t.repo_id
       ORDER BY t.priority ASC, t.created_at DESC`
    );
    return result.rows;
  });

  if (!r.ok) return r.panel;
  return <TasksView tasks={r.data} />;
}
