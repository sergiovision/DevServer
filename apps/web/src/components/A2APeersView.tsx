'use client';

import { useState } from 'react';
import {
  CAlert,
  CBadge,
  CButton,
  CCard,
  CCardBody,
  CCardHeader,
  CFormCheck,
  CFormInput,
  CFormSelect,
  CTable,
  CTableBody,
  CTableDataCell,
  CTableHead,
  CTableHeaderCell,
  CTableRow,
} from '@coreui/react-pro';
import { api, errorMessage } from '@/lib/api-client';
import { EmptyState } from '@/components/EmptyState';

interface Peer {
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

interface Props {
  peers: Peer[];
  repos: { id: number; name: string }[];
}

const TASK_TYPES = ['research', 'skill', 'coding', 'test', 'script'];

/**
 * Admin UI for A2A peers — external agents allowed to delegate work here.
 *
 * Creating a peer returns its bearer token exactly once; only the SHA-256 is
 * stored. The token is therefore shown in a dismissible banner that makes the
 * one-shot nature explicit, and there is no edit form for it (rotate = delete
 * and recreate), mirroring the webhook-trigger design.
 */
export function A2APeersView({ peers, repos }: Props) {
  const [items, setItems] = useState(peers);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [issued, setIssued] = useState<{ name: string; token: string } | null>(null);

  const [form, setForm] = useState({
    name: '',
    allowed_task_types: ['research'] as string[],
    allowed_repo_ids: [] as number[],
    require_plan_approval: true,
    force_git_flow: 'patch',
    daily_task_quota: 10,
    max_cost_usd: '' as string,
    max_wall_seconds: '' as string,
  });

  const baseUrl = typeof window !== 'undefined' ? window.location.origin : '';

  function toggleIn<T>(list: T[], value: T): T[] {
    return list.includes(value) ? list.filter((v) => v !== value) : [...list, value];
  }

  async function create() {
    if (!form.name.trim()) {
      setError('Name is required');
      return;
    }
    setCreating(true);
    setError(null);
    try {
      const data = await api.post<{ peer: Peer; token: string }>('/api/a2a-peers', {
        ...form,
        max_cost_usd: form.max_cost_usd ? Number(form.max_cost_usd) : null,
        max_wall_seconds: form.max_wall_seconds ? Number(form.max_wall_seconds) : null,
      });
      setItems([data.peer, ...items]);
      setIssued({ name: data.peer.name, token: data.token });
      setForm({ ...form, name: '' });
    } catch (e) {
      setError(errorMessage(e, String(e)));
    } finally {
      setCreating(false);
    }
  }

  async function toggle(id: number, enabled: boolean) {
    try {
      await api.patch(`/api/a2a-peers/${id}`, { enabled });
    } catch {
      return; // the switch stays where the server has it
    }
    setItems(items.map((p) => (p.id === id ? { ...p, enabled } : p)));
  }

  async function remove(id: number, name: string) {
    if (
      !window.confirm(
        `Revoke peer "${name}"? Its token stops working immediately. Tasks it already created are kept.`,
      )
    ) {
      return;
    }
    try {
      await api.delete(`/api/a2a-peers/${id}`);
    } catch {
      return; // the row stays: the peer was not revoked
    }
    setItems(items.filter((p) => p.id !== id));
  }

  return (
    <>
      {issued && (
        <CAlert color="warning" dismissible onClose={() => setIssued(null)}>
          <strong>Token for “{issued.name}” — shown once.</strong>
          <div className="small mt-1">
            Only its SHA-256 is stored. Copy it now; it cannot be recovered. To
            replace it, delete this peer and create a new one.
          </div>
          <code
            className="d-block mt-2 p-2 bg-body-secondary rounded"
            style={{ cursor: 'pointer', wordBreak: 'break-all' }}
            title="Click to copy"
            onClick={() => navigator.clipboard.writeText(issued.token)}
          >
            {issued.token}
          </code>
        </CAlert>
      )}

      <CCard className="mb-4">
        <CCardHeader>Issue a peer credential</CCardHeader>
        <CCardBody>
          <div className="row g-3">
            <div className="col-md-4">
              <label className="form-label small">Name</label>
              <CFormInput
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
                placeholder="orchestrator-agent"
              />
            </div>
            <div className="col-md-2">
              <label className="form-label small">Git flow</label>
              <CFormSelect
                value={form.force_git_flow}
                onChange={(e) => setForm({ ...form, force_git_flow: e.target.value })}
              >
                <option value="patch">patch (never pushes)</option>
                <option value="branch">branch (opens PRs)</option>
              </CFormSelect>
            </div>
            <div className="col-md-2">
              <label className="form-label small">Daily quota</label>
              <CFormInput
                type="number"
                min={0}
                value={form.daily_task_quota}
                onChange={(e) =>
                  setForm({ ...form, daily_task_quota: parseInt(e.target.value) || 0 })
                }
              />
            </div>
            <div className="col-md-2">
              <label
                className="form-label small"
                title="Recorded on each task but not enforced: A2A tasks run in subscription (max) billing mode, where cost is not metered. Use Max seconds/task for an enforced ceiling."
              >
                Max $/task <span className="text-muted">(advisory)</span>
              </label>
              <CFormInput
                type="number"
                step="0.5"
                min={0}
                value={form.max_cost_usd}
                onChange={(e) => setForm({ ...form, max_cost_usd: e.target.value })}
                placeholder="none"
              />
            </div>
            <div className="col-md-2">
              <label className="form-label small">Max seconds/task</label>
              <CFormInput
                type="number"
                min={0}
                value={form.max_wall_seconds}
                onChange={(e) => setForm({ ...form, max_wall_seconds: e.target.value })}
                placeholder="none"
              />
            </div>

            <div className="col-md-6">
              <label className="form-label small">Allowed task types</label>
              <div className="d-flex flex-wrap gap-3">
                {TASK_TYPES.map((t) => (
                  <CFormCheck
                    key={t}
                    id={`tt-${t}`}
                    label={t}
                    checked={form.allowed_task_types.includes(t)}
                    onChange={() =>
                      setForm({
                        ...form,
                        allowed_task_types: toggleIn(form.allowed_task_types, t),
                      })
                    }
                  />
                ))}
              </div>
            </div>

            <div className="col-md-6">
              <label className="form-label small">
                Allowed repos{' '}
                <span className="text-muted">(none = repo-less tasks only)</span>
              </label>
              <div className="d-flex flex-wrap gap-3">
                {repos.length === 0 && (
                  <span className="text-muted small">No active repos.</span>
                )}
                {repos.map((r) => (
                  <CFormCheck
                    key={r.id}
                    id={`repo-${r.id}`}
                    label={r.name}
                    checked={form.allowed_repo_ids.includes(r.id)}
                    onChange={() =>
                      setForm({
                        ...form,
                        allowed_repo_ids: toggleIn(form.allowed_repo_ids, r.id),
                      })
                    }
                  />
                ))}
              </div>
            </div>

            <div className="col-md-8 d-flex align-items-center">
              <CFormCheck
                id="require-approval"
                label="Require human plan approval before any code runs (recommended)"
                checked={form.require_plan_approval}
                onChange={(e) =>
                  setForm({ ...form, require_plan_approval: e.target.checked })
                }
              />
            </div>
            <div className="col-md-4 d-flex align-items-end justify-content-end">
              <CButton color="primary" onClick={create} disabled={creating}>
                {creating ? 'Issuing…' : 'Issue credential'}
              </CButton>
            </div>
          </div>
          {error && <div className="text-danger mt-2 small">{error}</div>}
        </CCardBody>
      </CCard>

      <CCard>
        <CCardHeader>
          Peers ({items.length})
          <span className="small text-muted ms-2">
            Agent Card: <code>{baseUrl}/.well-known/agent-card.json</code> · Endpoint:{' '}
            <code>{baseUrl}/api/a2a</code>
          </span>
        </CCardHeader>
        <CCardBody className="p-0">
          {items.length === 0 ? (
            <EmptyState
              title="No peers yet"
              hint="Issue a credential above to let an external agent discover this DevServer and delegate tasks to it over A2A."
            />
          ) : (
            <CTable hover className="mb-0">
              <CTableHead>
                <CTableRow>
                  <CTableHeaderCell>Peer</CTableHeaderCell>
                  <CTableHeaderCell>Scope</CTableHeaderCell>
                  <CTableHeaderCell>Governance</CTableHeaderCell>
                  <CTableHeaderCell className="text-center">Today</CTableHeaderCell>
                  <CTableHeaderCell className="text-center">Tasks</CTableHeaderCell>
                  <CTableHeaderCell className="text-center">Enabled</CTableHeaderCell>
                  <CTableHeaderCell />
                </CTableRow>
              </CTableHead>
              <CTableBody>
                {items.map((p) => {
                  const repoNames = (p.allowed_repo_ids || [])
                    .map((id) => repos.find((r) => r.id === id)?.name || `#${id}`)
                    .join(', ');
                  return (
                    <CTableRow key={p.id}>
                      <CTableDataCell>
                        <strong>{p.name}</strong>
                        <div className="small text-muted">
                          <code>a2a:{p.slug}</code>
                        </div>
                      </CTableDataCell>
                      <CTableDataCell className="small">
                        <div>{(p.allowed_task_types || []).join(', ')}</div>
                        <div className="text-muted">{repoNames || 'no repos'}</div>
                      </CTableDataCell>
                      <CTableDataCell className="small">
                        {p.require_plan_approval ? (
                          <CBadge color="success">plan approval</CBadge>
                        ) : (
                          <CBadge color="warning">autonomous</CBadge>
                        )}{' '}
                        <CBadge color="secondary">{p.force_git_flow}</CBadge>
                        <div className="text-muted mt-1">
                          {p.max_cost_usd ? `≤ $${p.max_cost_usd} (advisory)` : 'no cost cap'}
                          {p.max_wall_seconds ? ` · ≤ ${p.max_wall_seconds}s` : ''}
                        </div>
                      </CTableDataCell>
                      <CTableDataCell className="text-center">
                        {p.tasks_today}/{p.daily_task_quota}
                      </CTableDataCell>
                      <CTableDataCell className="text-center">{p.task_count}</CTableDataCell>
                      <CTableDataCell className="text-center">
                        <CFormCheck
                          checked={p.enabled}
                          onChange={(e) => toggle(p.id, e.target.checked)}
                        />
                      </CTableDataCell>
                      <CTableDataCell>
                        <CButton
                          color="danger"
                          variant="ghost"
                          size="sm"
                          onClick={() => remove(p.id, p.name)}
                        >
                          Revoke
                        </CButton>
                      </CTableDataCell>
                    </CTableRow>
                  );
                })}
              </CTableBody>
            </CTable>
          )}
        </CCardBody>
      </CCard>
    </>
  );
}
