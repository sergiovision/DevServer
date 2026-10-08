/**
 * POST /api/import/confluence
 *
 * Import selected Confluence pages as Tasks or Ideas — idempotently.
 *
 * Body: {
 *   ids: string[],                       // Confluence page ids
 *   repo_id?: number,                    // per-repo creds + task target repo
 *   target_type: 'task' | 'idea',
 *   task?: { priority?: number, enqueue?: boolean },
 *   idea?: { parent_id?: number | null },
 * }
 *
 * Flow: batch-fetch normalised drafts from the worker, then per draft consult
 * the external_imports ledger keyed on (source, external_id, target_type):
 *   - no row            → create target (+ enqueue for tasks) + ledger row
 *   - row, rev changed  → update target in place, bump ledger rev
 *   - row, same rev     → skip (no duplicates)
 * A ledger row whose target was deleted falls back to re-create.
 */
import { NextRequest, NextResponse } from 'next/server';
import { query } from '@/lib/db';
import { enqueueTask } from '@/lib/queue';
import { apiErrorResponse } from '@/lib/api-errors';
import { WORKER_URL } from '@/lib/worker-url';

interface Draft {
  source: string;
  external_id: string;
  external_rev: string;
  title: string;
  markdown: string;
  scope: string;
  source_url: string;
}

interface ImportResult {
  external_id: string;
  action: 'created' | 'updated' | 'skipped';
  target_type: string;
  target_id: number | null;
  task_key?: string;
  title: string;
}

/** Task description = page markdown + provenance footer for agent context. */
function taskDescription(d: Draft): string {
  return `${d.markdown}\n\n---\nImported from Confluence: ${d.source_url} (v${d.external_rev})`;
}

export async function POST(request: NextRequest) {
  try {
    const body = await request.json();
    const {
      ids = [],
      repo_id = null,
      target_type,
      task = {},
      idea = {},
    } = body as {
      ids: string[];
      repo_id: number | null;
      target_type: 'task' | 'idea';
      task?: { priority?: number; enqueue?: boolean };
      idea?: { parent_id?: number | null };
    };

    if (target_type !== 'task' && target_type !== 'idea') {
      return NextResponse.json(
        { error: "target_type must be 'task' or 'idea'" },
        { status: 400 },
      );
    }
    if (!Array.isArray(ids) || ids.length === 0) {
      return NextResponse.json({ error: 'ids must be a non-empty array' }, { status: 400 });
    }
    if (target_type === 'task' && !repo_id) {
      return NextResponse.json(
        { error: 'repo_id is required when importing as tasks' },
        { status: 400 },
      );
    }

    // 1. Batch-fetch normalised drafts (fetch + XHTML→Markdown) from the worker.
    let drafts: Draft[] = [];
    let fetchErrors: { external_id: string; error: string }[] = [];
    try {
      const res = await fetch(`${WORKER_URL}/internal/import/confluence/import`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids, repo_id }),
        cache: 'no-store',
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        return NextResponse.json(
          { error: data?.detail || `worker returned ${res.status}` },
          { status: res.status },
        );
      }
      drafts = data.drafts || [];
      fetchErrors = data.errors || [];
    } catch {
      return NextResponse.json({ error: 'worker unreachable' }, { status: 502 });
    }

    // 2. Resolve each draft against the external_imports ledger.
    const results: ImportResult[] = [];
    for (const draft of drafts) {
      const ledger = await query(
        `SELECT id, external_rev, target_id FROM external_imports
         WHERE source = 'confluence' AND external_id = $1 AND target_type = $2`,
        [draft.external_id, target_type],
      );
      const existing = ledger.rows[0] as
        | { id: number; external_rev: string; target_id: number | null }
        | undefined;

      // Does the previously-imported target still exist?
      let target: Record<string, unknown> | null = null;
      if (existing?.target_id) {
        const table = target_type === 'task' ? 'tasks' : 'ideas';
        const t = await query(`SELECT * FROM ${table} WHERE id = $1`, [existing.target_id]);
        target = t.rows[0] ?? null;
      }

      if (existing && target && existing.external_rev === draft.external_rev) {
        results.push({
          external_id: draft.external_id,
          action: 'skipped',
          target_type,
          target_id: existing.target_id,
          task_key: target_type === 'task' ? (target.task_key as string) : undefined,
          title: draft.title,
        });
        continue;
      }

      if (existing && target) {
        // Version changed → update the target in place.
        if (target_type === 'task') {
          await query(
            `UPDATE tasks SET title = $1, description = $2, updated_at = NOW() WHERE id = $3`,
            [draft.title, taskDescription(draft), existing.target_id],
          );
        } else {
          await query(
            `UPDATE ideas SET title = $1, content = $2, updated_at = NOW() WHERE id = $3`,
            [draft.title, draft.markdown, existing.target_id],
          );
        }
        await query(
          `UPDATE external_imports
           SET external_rev = $1, title = $2, scope = $3, source_url = $4, updated_at = NOW()
           WHERE id = $5`,
          [draft.external_rev, draft.title, draft.scope, draft.source_url, existing.id],
        );
        results.push({
          external_id: draft.external_id,
          action: 'updated',
          target_type,
          target_id: existing.target_id,
          task_key: target_type === 'task' ? (target.task_key as string) : undefined,
          title: draft.title,
        });
        continue;
      }

      // Create (fresh import, or ledger row whose target was deleted).
      let targetId: number;
      let taskKey: string | undefined;
      if (target_type === 'task') {
        taskKey = `CONF-${draft.external_id}`;
        const priority = Number(task.priority) || 3;
        const inserted = await query(
          `INSERT INTO tasks (repo_id, task_key, title, description, priority, labels,
                              mode, task_type, claude_mode, agent_vendor, git_flow, status)
           VALUES ($1, $2, $3, $4, $5, $6, 'autonomous', 'coding', 'max', 'anthropic', 'branch', 'pending')
           ON CONFLICT (repo_id, task_key) DO UPDATE
             SET title = EXCLUDED.title, description = EXCLUDED.description, updated_at = NOW()
           RETURNING id, status`,
          [repo_id, taskKey, draft.title, taskDescription(draft), priority, ['confluence']],
        );
        targetId = inserted.rows[0].id as number;

        if (task.enqueue !== false && inserted.rows[0].status === 'pending') {
          const jobId = await enqueueTask({
            taskId: targetId,
            repoId: repo_id as number,
            taskKey,
            title: draft.title,
            priority,
            mode: 'autonomous',
            claudeMode: 'max',
            maxTurns: null,
            gitFlow: 'branch',
          });
          await query(
            `UPDATE tasks SET status = 'queued', queue_job_id = $1, updated_at = NOW() WHERE id = $2`,
            [jobId, targetId],
          );
        }
      } else {
        const inserted = await query(
          `INSERT INTO ideas (parent_id, kind, title, content)
           VALUES ($1, 'idea', $2, $3)
           RETURNING id`,
          [idea.parent_id ?? null, draft.title, draft.markdown],
        );
        targetId = inserted.rows[0].id as number;
      }

      await query(
        `INSERT INTO external_imports (source, external_id, external_rev, scope,
                                       target_type, target_id, title, source_url)
         VALUES ('confluence', $1, $2, $3, $4, $5, $6, $7)
         ON CONFLICT (source, external_id, target_type) DO UPDATE
           SET external_rev = EXCLUDED.external_rev, scope = EXCLUDED.scope,
               target_id = EXCLUDED.target_id, title = EXCLUDED.title,
               source_url = EXCLUDED.source_url, updated_at = NOW()`,
        [draft.external_id, draft.external_rev, draft.scope, target_type, targetId, draft.title, draft.source_url],
      );

      results.push({
        external_id: draft.external_id,
        action: 'created',
        target_type,
        target_id: targetId,
        task_key: taskKey,
        title: draft.title,
      });
    }

    return NextResponse.json({ results, errors: fetchErrors });
  } catch (err) {
    return apiErrorResponse(err, 'POST /api/import/confluence');
  }
}
