import { query } from '@/lib/db';
import { TaskForm } from '@/components/TaskForm';
import type { Repo } from '@/lib/types';

export const dynamic = 'force-dynamic';

export default async function NewTaskPage() {
  let repos: Repo[] = [];

  try {
    const result = await query<Repo>(
      'SELECT * FROM repos WHERE active = true ORDER BY name'
    );
    repos = result.rows;
  } catch (err) {
    console.error('Failed to fetch repos:', err);
  }

  return (
    <>
      <h2 className="mb-4">Create New Task</h2>
      <TaskForm repos={repos} />
    </>
  );
}
