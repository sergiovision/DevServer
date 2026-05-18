import type { ReactElement } from 'react';
import { isDatabaseError } from './db-errors';
import { DbErrorPanel } from '@/components/DbErrorPanel';

/**
 * Wrap a Server Component's data-loading block. If the load throws a
 * DatabaseError (Postgres down / wrong creds / missing DB / no perms…),
 * returns `{ ok: false, panel }` so the page can early-return the diagnostic
 * panel instead of rendering with empty / default data.
 *
 * Non-DB errors are re-thrown so the Next.js error boundary handles them.
 *
 * Usage:
 *
 *   const r = await tryDbPage(async () => {
 *     const tasks = await query(...);
 *     const stats = await query(...);
 *     return { tasks: tasks.rows, stats: stats.rows };
 *   });
 *   if (!r.ok) return r.panel;
 *   const { tasks, stats } = r.data;
 */
export async function tryDbPage<T>(
  load: () => Promise<T>,
): Promise<{ ok: true; data: T } | { ok: false; panel: ReactElement }> {
  try {
    const data = await load();
    return { ok: true, data };
  } catch (err) {
    if (isDatabaseError(err)) {
      return { ok: false, panel: <DbErrorPanel info={err.info} /> };
    }
    throw err;
  }
}
