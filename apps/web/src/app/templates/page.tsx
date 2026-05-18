import { query } from '@/lib/db';
import { tryDbPage } from '@/lib/db-page';
import type { TaskTemplate } from '@/lib/types';
import { TemplateList } from '@/components/TemplateList';

export const dynamic = 'force-dynamic';

export default async function TemplatesPage() {
  const r = await tryDbPage(async () => {
    const result = await query<TaskTemplate>(
      'SELECT * FROM task_templates ORDER BY name ASC',
    );
    return result.rows;
  });

  if (!r.ok) return r.panel;

  return (
    <>
      <div className="d-flex justify-content-between align-items-center mb-4">
        <h2 className="mb-0">Task Templates</h2>
      </div>
      <TemplateList templates={r.data} />
    </>
  );
}
