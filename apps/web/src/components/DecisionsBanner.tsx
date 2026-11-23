'use client';

/**
 * Open side-effect decision points (HITL gate) with Approve/Reject.
 *
 * Polls `/api/decisions`, a different resource from the sidebar's Needs You
 * counts (`lib/needs-you.ts`), so it keeps its own — faster — poll; an agent
 * is suspended until someone answers. It goes through `useResource`, so the
 * poll pauses while the tab is hidden and reloads when connectivity returns.
 */

import { useCallback, useState } from 'react';
import {
  CCard,
  CCardHeader,
  CCardBody,
  CBadge,
  CButton,
  CListGroup,
  CListGroupItem,
} from '@coreui/react-pro';
import { api, errorMessage } from '@/lib/api-client';
import { useResource } from '@/lib/use-async';
import { needsYouPoll } from '@/lib/needs-you';

const DECISIONS_POLL_MS = 5000;

interface DecisionPoint {
  id: number;
  task_id: number;
  task_key: string;
  title: string;
  kind: string;
  severity: string;
  proposed_action: string;
  status: string;
  created_at: string;
}

const KIND_COLOR: Record<string, string> = {
  spend_money: 'danger',
  send_message: 'warning',
  publish: 'warning',
  clinical: 'danger',
  legal: 'danger',
  irreversible: 'danger',
  ambiguous: 'secondary',
};

/**
 * Renders nothing when there are none, so it's invisible unless the
 * `side_effect_gate` feature is in use.
 */
export function DecisionsBanner() {
  const [busyId, setBusyId] = useState<number | null>(null);
  // Errors are deliberately silent: the worker may be down, and the banner
  // simply does not show.
  const { data, reload } = useResource(
    async (signal) => {
      const body = await api.get<{ decisions?: unknown }>('/api/decisions', { signal });
      return Array.isArray(body?.decisions) ? (body.decisions as DecisionPoint[]) : [];
    },
    [],
    { pollMs: DECISIONS_POLL_MS },
  );
  const decisions = data ?? [];

  const resolve = useCallback(
    async (id: number, decision: 'approve' | 'reject') => {
      if (decision === 'reject' && !window.confirm('Reject this action? The agent will be told not to perform it.')) {
        return;
      }
      setBusyId(id);
      try {
        await api.post(`/api/decisions/${id}/resolve`, { decision });
        await reload();
        // Resolving unblocks a task; refresh the shared pill instead of
        // leaving it stale until the next tick (no second poll is started).
        void needsYouPoll.refresh();
      } catch (err) {
        alert(`Resolve failed: ${errorMessage(err)}`);
      } finally {
        setBusyId(null);
      }
    },
    [reload],
  );

  if (decisions.length === 0) return null;

  return (
    <CCard className="mb-4 border-warning">
      <CCardHeader className="bg-warning-subtle">
        <strong>🛑 Approvals needed ({decisions.length})</strong>
      </CCardHeader>
      <CCardBody>
        <CListGroup>
          {decisions.map((d) => (
            <CListGroupItem key={d.id} className="d-flex justify-content-between align-items-center gap-2">
              <div className="flex-grow-1">
                <div className="d-flex align-items-center gap-2 mb-1">
                  <CBadge color={KIND_COLOR[d.kind] ?? 'secondary'}>{d.kind}</CBadge>
                  <a href={`/tasks/${d.task_id}`}><strong>{d.task_key}</strong></a>
                  <span className="text-body-secondary text-truncate">{d.title}</span>
                </div>
                <div className="small text-body-secondary">{d.proposed_action}</div>
              </div>
              <div className="d-flex gap-2 flex-shrink-0">
                <CButton
                  color="success"
                  size="sm"
                  disabled={busyId === d.id}
                  onClick={() => resolve(d.id, 'approve')}
                >
                  Approve
                </CButton>
                <CButton
                  color="outline-danger"
                  size="sm"
                  disabled={busyId === d.id}
                  onClick={() => resolve(d.id, 'reject')}
                >
                  Reject
                </CButton>
              </div>
            </CListGroupItem>
          ))}
        </CListGroup>
      </CCardBody>
    </CCard>
  );
}
