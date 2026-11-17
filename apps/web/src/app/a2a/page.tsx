import { query } from '@/lib/db';
import { tryDbPage } from '@/lib/db-page';
import { A2APeersView } from '@/components/A2APeersView';

export const dynamic = 'force-dynamic';

interface PeerRow {
  id: number;
  name: string;
  slug: string;
  allowed_repo_ids: number[];
  allowed_task_types: string[];
  require_plan_approval: boolean;
  force_git_flow: string;
  max_cost_usd: string | null;
  max_wall_seconds: number | null;
  daily_task_quota: number;
  tasks_today: number;
  enabled: boolean;
  last_seen_at: string | null;
  call_count: number;
  task_count: number;
}

export default async function A2APage() {
  const r = await tryDbPage(async () => {
    const pr = await query<PeerRow>(
      `SELECT id, name, slug, allowed_repo_ids, allowed_task_types,
              require_plan_approval, force_git_flow, max_cost_usd,
              max_wall_seconds, daily_task_quota, tasks_today, enabled,
              last_seen_at, call_count, task_count
         FROM a2a_peers ORDER BY created_at DESC`,
    );
    const rr = await query<{ id: number; name: string }>(
      `SELECT id, name FROM repos WHERE active = TRUE ORDER BY name`,
    );
    return { peers: pr.rows, repos: rr.rows };
  });

  if (!r.ok) return r.panel;

  return (
    <>
      <div className="d-flex justify-content-between align-items-center mb-2">
        <h2 className="mb-0">Agent Peers (A2A)</h2>
      </div>
      <p className="text-body-secondary small mb-4">
        External agents that may delegate work to this DevServer over the{' '}
        <a
          href="https://a2a-protocol.org/latest/specification/"
          target="_blank"
          rel="noreferrer"
        >
          Agent2Agent
        </a>{' '}
        protocol. Each credential carries its own scope and spend ceiling, and
        every task it creates is tagged <code>a2a:&lt;slug&gt;</code> in the task
        history.
      </p>
      <A2APeersView peers={r.data.peers} repos={r.data.repos} />
    </>
  );
}
