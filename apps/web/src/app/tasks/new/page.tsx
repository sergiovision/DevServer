import { query } from '@/lib/db';
import { TaskForm } from '@/components/TaskForm';
import type { Repo, TaskTemplate } from '@/lib/types';

export const dynamic = 'force-dynamic';

export default async function NewTaskPage() {
  let repos: Repo[] = [];
  let templates: TaskTemplate[] = [];

  try {
    const [repoResult, templateResult] = await Promise.all([
      query<Repo>('SELECT * FROM repos WHERE active = true ORDER BY name'),
      query<TaskTemplate>('SELECT * FROM task_templates ORDER BY name ASC'),
    ]);
    repos = repoResult.rows;
    templates = templateResult.rows;
  } catch (err) {
    console.error('Failed to fetch repos/templates:', err);
  }

  return (
    <>
      <h2 className="mb-4">Create New Task</h2>
      <TaskForm repos={repos} templates={templates} />
    </>
  );
}
