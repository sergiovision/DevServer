import { query } from '@/lib/db';
import { tryDbPage } from '@/lib/db-page';
import { TaskForm } from '@/components/TaskForm';
import type { Repo, TaskTemplate } from '@/lib/types';

export const dynamic = 'force-dynamic';

export default async function NewTaskPage() {
  const r = await tryDbPage(async () => {
    const [repoResult, templateResult] = await Promise.all([
      query<Repo>('SELECT * FROM repos WHERE active = true ORDER BY name'),
      query<TaskTemplate>('SELECT * FROM task_templates ORDER BY name ASC'),
    ]);
    return { repos: repoResult.rows, templates: templateResult.rows };
  });

  if (!r.ok) return r.panel;

  return (
    <>
      <h2 className="mb-4">Create New Task</h2>
      <TaskForm repos={r.data.repos} templates={r.data.templates} />
    </>
  );
}
