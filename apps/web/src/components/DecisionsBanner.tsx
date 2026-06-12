'use client';

import { useCallback, useEffect, useState } from 'react';
import {
  CCard,
  CCardHeader,
  CCardBody,
  CBadge,
  CButton,
  CListGroup,
  CListGroupItem,
} from '@coreui/react-pro';

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
 * Shows open side-effect decision points (HITL gate) with Approve/Reject.
 * Renders nothing when there are none, so it's invisible unless the
 * `side_effect_gate` feature is in use.
 */
export function DecisionsBanner() {
  const [decisions, setDecisions] = useState<DecisionPoint[]>([]);
  const [busyId, setBusyId] = useState<number | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await fetch('/api/decisions', { cache: 'no-store' });
      if (!res.ok) return;
      const data = await res.json();
      setDecisions(Array.isArray(data?.decisions) ? data.decisions : []);
    } catch {
      // worker may be down — stay quiet, the banner just won't show.
    }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, [load]);

  const resolve = useCallback(
    async (id: number, decision: 'approve' | 'reject') => {
      if (decision === 'reject' && !window.confirm('Reject this action? The agent will be told not to perform it.')) {
        return;
      }
      setBusyId(id);
      try {
        const res = await fetch(`/api/decisions/${id}/resolve`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ decision }),
        });
        const data = await res.json().catch(() => null);
        if (!res.ok) {
          alert(`Resolve failed: ${data?.error || res.statusText}`);
          return;
        }
        await load();
      } finally {
        setBusyId(null);
      }
    },
    [load],
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
