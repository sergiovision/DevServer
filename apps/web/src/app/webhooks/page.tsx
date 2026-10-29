import { query } from '@/lib/db';
import { tryDbPage } from '@/lib/db-page';
import { listTriggers } from '@/lib/webhooks/triggers';
import { WebhookTriggerList } from '@/components/WebhookTriggerList';

export const dynamic = 'force-dynamic';

export default async function WebhooksPage() {
  const r = await tryDbPage(async () => {
    const [triggers, repos, templates] = await Promise.all([
      listTriggers(),
      query<{ id: number; name: string }>('SELECT id, name FROM repos WHERE active ORDER BY name ASC'),
      query<{ id: number; name: string }>('SELECT id, name FROM task_templates ORDER BY name ASC'),
    ]);
    return { triggers, repos: repos.rows, templates: templates.rows };
  });

  if (!r.ok) return r.panel;

  return (
    <>
      <div className="d-flex justify-content-between align-items-center mb-4">
        <h2 className="mb-0">Webhooks</h2>
      </div>
      <WebhookTriggerList
        triggers={r.data.triggers}
        repos={r.data.repos}
        templates={r.data.templates}
      />
    </>
  );
}
