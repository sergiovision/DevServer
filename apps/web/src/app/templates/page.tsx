import { query } from '@/lib/db';
import type { TaskTemplate } from '@/lib/types';
import { TemplateList } from '@/components/TemplateList';

export const dynamic = 'force-dynamic';

export default async function TemplatesPage() {
  let templates: TaskTemplate[] = [];

  try {
    const result = await query<TaskTemplate>(
      'SELECT * FROM task_templates ORDER BY name ASC',
    );
    templates = result.rows;
  } catch (err) {
    console.error('Failed to fetch templates:', err);
  }

  return (
    <>
      <div className="d-flex justify-content-between align-items-center mb-4">
        <h2 className="mb-0">Task Templates</h2>
      </div>
      <TemplateList templates={templates} />
    </>
  );
}
