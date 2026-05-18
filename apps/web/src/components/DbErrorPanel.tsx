'use client';

import { CAlert, CCard, CCardBody, CCardHeader } from '@coreui/react-pro';
import type { DbErrorInfo } from '@/lib/db-errors';

/**
 * Inline panel rendered by Server Component pages when their initial DB
 * query fails. Lives next to PageContent rather than swapping the whole
 * layout — the sidebar/header still work even when the database is down.
 *
 * Pair with `tryDbPage` in `lib/db-page.tsx` so a failing query early-
 * returns this panel instead of rendering an empty page.
 */
export function DbErrorPanel({ info }: { info: DbErrorInfo }) {
  return (
    <CCard className="border-danger">
      <CCardHeader className="bg-danger text-white">
        Database unavailable
      </CCardHeader>
      <CCardBody>
        <CAlert color="danger" className="mb-3">
          <div style={{ fontWeight: 600 }}>{info.userMessage}</div>
          {info.hint && <div className="mt-2">{info.hint}</div>}
        </CAlert>
        <div className="text-medium-emphasis small">
          <div><strong>kind:</strong> <code>{info.kind}</code></div>
          <div><strong>code:</strong> <code>{info.code}</code></div>
        </div>
      </CCardBody>
    </CCard>
  );
}
