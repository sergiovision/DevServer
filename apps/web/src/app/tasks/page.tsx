import { query } from '@/lib/db';
import { TasksView } from '@/components/TasksView';
import type { Task } from '@/lib/types';

export const dynamic = 'force-dynamic';

export default async function TasksPage() {
  let tasks: Task[] = [];

  try {
    const result = await query<Task>(
      `SELECT t.*, r.name as repo_name FROM tasks t
       LEFT JOIN repos r ON r.id = t.repo_id
       ORDER BY t.priority ASC, t.created_at DESC`
    );
    tasks = result.rows;
  } catch (err) {
    console.error('Failed to fetch tasks:', err);
  }

  return <TasksView tasks={tasks} />;
}
