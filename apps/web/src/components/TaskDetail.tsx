'use client';

import React, { useCallback, useState } from 'react';
import { useRouter } from 'next/navigation';
import {
  CCard,
  CCardBody,
  CCardHeader,
  CAlert,
  CBadge,
  CButton,
  CTable,
  CTableHead,
  CTableRow,
  CTableHeaderCell,
  CTableBody,
  CTableDataCell,
  CFormLabel,
  CFormTextarea,
  CFormCheck,
  CFormSelect,
  CRow,
  CCol,
} from '@coreui/react-pro';
import { AgentLog } from './AgentLog';
import { TaskLog } from './TaskLog';
import { MaxTurnsInput } from './MaxTurnsInput';
import { VendorModelPicker } from './VendorModelPicker';
import { PatchesPanel, MessagesPanel } from './pro-loader';
import type { Task, TaskRun, TaskEvent, TaskStatus, GhostJobInfo, GitFlow, AgentVendor, ClaudeMode } from '@/lib/types';
import { STATUS_COLORS } from '@/lib/types';

interface TaskDetailProps {
  task: Task;
  runs: TaskRun[];
  events: TaskEvent[];
  ghost?: GhostJobInfo | null;
}

export function TaskDetail({ task, runs, events, ghost }: TaskDetailProps) {
  const router = useRouter();

  // Description / acceptance editable state
  const [description, setDescription] = useState(task.description ?? '');
  // Clean acceptance criteria from JSON formatting if present
  const cleanAcceptance = (acceptance: string | null): string => {
    if (!acceptance) return '';
    // Remove JSON brackets and quotes if the content is wrapped in {"..."}
    const trimmed = acceptance.trim();
    if (trimmed.startsWith('{"') && trimmed.endsWith('"}')) {
      return trimmed.slice(2, -2);
    }
    return trimmed;
  };
  const [acceptance, setAcceptance] = useState(cleanAcceptance(task.acceptance));
  const [descSaving, setDescSaving] = useState(false);
  const [descError, setDescError] = useState('');
  const [descSaved, setDescSaved] = useState(false);

  const handleSaveDesc = useCallback(async () => {
    setDescSaving(true);
    setDescError('');
    setDescSaved(false);
    try {
      const res = await fetch(`/api/tasks/${task.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ description: description || null, acceptance: acceptance || null }),
      });
      if (!res.ok) {
        const d = await res.json();
        throw new Error(d.error || 'Save failed');
      }
      setDescSaved(true);
      router.refresh();
    } catch (err: unknown) {
      setDescError(err instanceof Error ? err.message : 'Save failed');
    } finally {
      setDescSaving(false);
    }
  }, [task.id, description, acceptance, router]);

  // Agent settings local edit state
  const [agentMaxTurns, setAgentMaxTurns] = useState<number | null>(task.max_turns ?? 50);
  const [agentVendor, setAgentVendor] = useState<AgentVendor>((task.agent_vendor ?? 'anthropic') as AgentVendor);
  const [agentModel, setAgentModel] = useState<string>(task.claude_model ?? '');
  const [agentBilling, setAgentBilling] = useState<ClaudeMode>((task.claude_mode ?? 'max') as ClaudeMode);
  const [agentGitFlow, setAgentGitFlow] = useState<GitFlow>((task.git_flow ?? 'branch') as GitFlow);
  const [backupVendor, setBackupVendor] = useState<AgentVendor>((task.backup_vendor ?? 'anthropic') as AgentVendor);
  const [backupModel, setBackupModel] = useState<string>(task.backup_model ?? 'claude-sonnet-4-6');
  const [agentSaving, setAgentSaving] = useState(false);
  const [agentError, setAgentError] = useState('');
  const [agentSaved, setAgentSaved] = useState(false);

  const handleSaveAgent = useCallback(async () => {
    setAgentSaving(true);
    setAgentError('');
    setAgentSaved(false);
    try {
      const res = await fetch(`/api/tasks/${task.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          max_turns: agentMaxTurns,
          agent_vendor: agentVendor,
          claude_model: agentModel.trim() || null,
          claude_mode: agentBilling,
          git_flow: agentGitFlow,
          backup_vendor: backupVendor !== agentVendor ? backupVendor : null,
          backup_model: backupModel.trim() || null,
        }),
      });
      if (!res.ok) {
        const d = await res.json();
        throw new Error(d.error || 'Save failed');
      }
      setAgentSaved(true);
      router.refresh();
    } catch (err: unknown) {
      setAgentError(err instanceof Error ? err.message : 'Save failed');
    } finally {
      setAgentSaving(false);
    }
  }, [task.id, agentMaxTurns, agentVendor, agentModel, agentBilling, agentGitFlow, backupVendor, backupModel, router]);

  const handleDelete = useCallback(async () => {
    if (!confirm(`Delete task "${task.task_key}"? This cannot be undone.`)) return;
    await fetch(`/api/tasks/${task.id}`, { method: 'DELETE' });
    router.push('/tasks');
  }, [task.id, task.task_key, router]);

  const handleFixGhost = useCallback(async () => {
    await fetch('/api/worker', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'restart' }) });
    router.refresh();
  }, [router]);

  const handleForceReopen = useCallback(async () => {
    if (!confirm('This will reset the task to pending, clear all locks, and restart the worker. Continue?')) return;
    // 1. Reset task status and clear job reference
    await fetch(`/api/tasks/${task.id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status: 'pending', queue_job_id: null }),
    });
    // 2. Clean locks + restart worker
    await fetch('/api/worker', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'restart' }),
    });
    router.refresh();
  }, [task.id, router]);

  const handleEnqueue = useCallback(async () => {
    await fetch(`/api/tasks/${task.id}/enqueue`, { method: 'POST' });
    router.refresh();
  }, [task.id, router]);

  const handleCancel = useCallback(async () => {
    await fetch(`/api/tasks/${task.id}/cancel`, { method: 'POST' });
    router.refresh();
  }, [task.id, router]);

  const [continuing, setContinuing] = useState(false);
  const handleContinue = useCallback(async () => {
    if (!confirm('Continue this task with the current primary model? The in-flight run will be stopped and a new session will resume from where it left off.')) return;
    setContinuing(true);
    try {
      const res = await fetch(`/api/tasks/${task.id}/continue`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model: null, mode: null }),
      });
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        alert(d.error || 'Continue failed');
      }
      router.refresh();
    } finally {
      setContinuing(false);
    }
  }, [task.id, router]);

  const [compacting, setCompacting] = useState(false);
  const handleCompact = useCallback(async () => {
    if (
      !confirm(
        "Summarise this task's prior transcript via the system LLM, replacing the per-retry context with a distilled summary? The next attempt will start with a fresh session.",
      )
    )
      return;
    setCompacting(true);
    try {
      const res = await fetch(`/api/tasks/${task.id}/compact`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ reason: 'manual_ui' }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        alert(data.error || 'Compaction failed');
      } else {
        alert(
          `Compacted ${data.chars_in ?? '?'} chars → ${data.chars_out ?? '?'} chars. Use "Continue" to resume with the summary.`,
        );
      }
      router.refresh();
    } finally {
      setCompacting(false);
    }
  }, [task.id, router]);

  const handleRetire = useCallback(async () => {
    await fetch(`/api/tasks/${task.id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status: 'retired' }),
    });
    router.refresh();
  }, [task.id, router]);

  const handleReopen = useCallback(async () => {
    await fetch(`/api/tasks/${task.id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status: 'pending' }),
    });
    router.refresh();
  }, [task.id, router]);

  const [refreshing, setRefreshing] = useState(false);
  const [refreshMsg, setRefreshMsg] = useState<{ type: 'success' | 'danger'; text: string } | null>(null);
  const handleRefreshGit = useCallback(async () => {
    if (!task.repo_id) return;
    setRefreshing(true);
    setRefreshMsg(null);
    try {
      const res = await fetch(`/api/repos/${task.repo_id}/refresh-git`, { method: 'POST' });
      if (!res.ok) {
        const data = await res.json().catch(() => null);
        throw new Error(data?.error || 'Refresh failed');
      }
      const data = await res.json();
      setRefreshMsg({ type: 'success', text: data.message });
    } catch (err) {
      setRefreshMsg({ type: 'danger', text: err instanceof Error ? err.message : 'Refresh failed' });
    } finally {
      setRefreshing(false);
    }
  }, [task.repo_id]);

  const [filling, setFilling] = useState(false);
  const [fillError, setFillError] = useState('');

  const handleFillTask = useCallback(async () => {
    if (!description.trim()) return;
    setFilling(true);
    setFillError('');
    try {
      const res = await fetch('/api/tasks/generate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ description }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => null);
        throw new Error(data?.error || 'devtask skill failed');
      }
      const generated = await res.json();
      if (generated.description) setDescription(generated.description);
      if (generated.acceptance) setAcceptance(generated.acceptance);
      // Patch remaining fields on server (never overwrite title/key on existing tasks)
      await fetch(`/api/tasks/${task.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          description: generated.description || undefined,
          acceptance: generated.acceptance || undefined,
          claude_model: generated.claude_model,
          max_turns: generated.max_turns,
          claude_mode: generated.claude_mode || undefined,
          skip_verify: generated.skip_verify,
        }),
      });
      router.refresh();
    } catch (err) {
      setFillError(err instanceof Error ? err.message : 'devtask skill failed');
    } finally {
      setFilling(false);
    }
  }, [description, task.id, router]);

  return (
    <>
      {ghost?.detected && (
        <CAlert color="warning" className="mb-4">
          <strong>Ghost job detected.</strong> This task is stuck in the queue but the worker is not processing it
          {ghost.lock_held ? ' (repo lock is held)' : ''}.
          This happens when the worker crashes mid-task.
          <CButton color="warning" size="sm" className="ms-3" onClick={handleFixGhost}>
            Fix: Restart Worker
          </CButton>
        </CAlert>
      )}
      <div className="mb-4">
        <h2 className="mb-1">{task.task_key}: {task.title}</h2>
        <div className="mb-2">
          <CBadge color={STATUS_COLORS[task.status as TaskStatus]}>
            {task.status}
          </CBadge>
          {task.repo_name && (
            <span className="ms-2 text-body-secondary">{task.repo_name}</span>
          )}
        </div>
        <div className="d-flex flex-wrap gap-2">
          <CButton
            color="outline-secondary"
            onClick={() => router.push('/tasks')}
          >
            Back to Tasks
          </CButton>
          {task.status === 'pending' && (
            <CButton color="success" onClick={handleEnqueue}>
              Enqueue
            </CButton>
          )}
          {task.status === 'test' && (
            <CButton color="info" onClick={handleReopen}>
              Reopen
            </CButton>
          )}
          {(task.status === 'running' || task.status === 'queued') && (
            <CButton color="danger" onClick={handleCancel}>
              Cancel
            </CButton>
          )}
          {(['running', 'queued', 'failed', 'blocked', 'cancelled', 'test'].includes(task.status)) && (
            <CButton
              color="info"
              disabled={continuing}
              onClick={handleContinue}
              title={
                task.status === 'test'
                  ? 'Resume this task with the existing session & branch to address a follow-up request from the Messages panel'
                  : 'Resume this task from where it left off'
              }
            >
              {continuing ? 'Continuing…' : 'Continue'}
            </CButton>
          )}
          {(['running', 'failed', 'blocked', 'cancelled'].includes(task.status)) && (
            <CButton
              color="secondary"
              variant="outline"
              disabled={compacting}
              onClick={handleCompact}
              title="Summarise prior attempts via the system LLM — next retry starts fresh with the summary"
            >
              {compacting ? 'Compacting…' : 'Compact'}
            </CButton>
          )}
          {(task.status === 'running' || task.status === 'verifying' || task.status === 'failed' || task.status === 'queued') && (
            <CButton color="warning" onClick={handleForceReopen}>
              Force Reopen
            </CButton>
          )}
          {(task.status === 'test' || task.status === 'failed' || task.status === 'cancelled') && (
            <CButton color="dark" variant="outline" onClick={handleRetire}>
              Retire
            </CButton>
          )}
          <CButton
            color="outline-danger"
            onClick={handleDelete}
          >
            Delete
          </CButton>
          {task.repo_clone_url && (
            <CButton
              color="outline-primary"
              href={task.repo_clone_url.replace(/\.git$/, '')}
              target="_blank"
              rel="noopener noreferrer"
              as="a"
            >
              Repo Link
            </CButton>
          )}
          {task.repo_id && (
            <CButton
              color="outline-info"
              disabled={refreshing}
              onClick={handleRefreshGit}
            >
              {refreshing ? 'Refreshing...' : 'Refresh Git'}
            </CButton>
          )}
          {refreshMsg && (
            <span className={`small text-${refreshMsg.type} align-self-center`}>{refreshMsg.text}</span>
          )}
        </div>
      </div>

      <CRow>
        <CCol lg={8}>
          {/* Description + Acceptance — editable */}
          <CCard className="mb-4">
            <CCardHeader><strong>Description &amp; Acceptance Criteria</strong></CCardHeader>
            <CCardBody>
              {descError && <CAlert color="danger" className="py-2">{descError}</CAlert>}
              {descSaved && <CAlert color="success" className="py-2">Saved.</CAlert>}
              <div className="mb-3">
                <div className="d-flex align-items-center gap-2 mb-1">
                  <CFormLabel className="mb-0">Description</CFormLabel>
                  {task.status !== 'retired' && (
                    <CButton
                      type="button"
                      color="info"
                      variant="outline"
                      size="sm"
                      disabled={filling || !description.trim()}
                      onClick={handleFillTask}
                    >
                      {filling ? 'Filling...' : 'Fill Task'}
                    </CButton>
                  )}
                  {fillError && (
                    <span className="text-danger small">{fillError}</span>
                  )}
                </div>
                <CFormTextarea
                  value={description}
                  onChange={(e) => setDescription(e.target.value)}
                  rows={5}
                  placeholder="Detailed task description…"
                />
              </div>
              <div className="mb-3">
                <CFormLabel className="mb-1">Acceptance Criteria</CFormLabel>
                <CFormTextarea
                  value={acceptance}
                  onChange={(e) => setAcceptance(e.target.value)}
                  rows={3}
                  placeholder="What conditions must be met for this task to be considered done?"
                />
              </div>
              <CButton color="primary" size="sm" disabled={descSaving} onClick={handleSaveDesc}>
                {descSaving ? 'Saving…' : 'Save'}
              </CButton>
            </CCardBody>
          </CCard>

          {/* Messages — two-way chat between operator and agent via task_messages.
              Placed above the Event Log so the operator can easily talk to the
              agent without scrolling the right-hand sidebar. */}
          <MessagesPanel taskId={task.id} taskKey={task.task_key} />

          {/* Live Log */}
          <CCard className="mb-4">
            <CCardHeader><strong>Event Log</strong></CCardHeader>
            <CCardBody>
              <AgentLog events={events} maxHeight="400px" />
            </CCardBody>
          </CCard>

          {/* File-based Task Log */}
          <CCard className="mb-4">
            <CCardHeader><strong>Task Log</strong></CCardHeader>
            <CCardBody>
              <TaskLog taskKey={task.task_key} maxHeight="600px" />
            </CCardBody>
          </CCard>
        </CCol>

        <CCol lg={4}>
          {/* Task Info */}
          <CCard className="mb-4">
            <CCardHeader><strong>Details</strong></CCardHeader>
            <CCardBody>
              <dl className="mb-0">
                <dt>Billing</dt>
                <dd>{task.claude_mode === 'max' ? 'Max (subscription)' : 'API Platform'}</dd>
                <dt>Queue Job ID</dt>
                <dd className="text-break">{task.queue_job_id || '-'}</dd>
                <dt>Created</dt>
                <dd suppressHydrationWarning>{new Date(task.created_at).toLocaleString()}</dd>
              </dl>
            </CCardBody>
          </CCard>

          {/* Agent Settings */}
          <CCard className="mb-4">
            <CCardHeader><strong>Agent Settings</strong></CCardHeader>
            <CCardBody>
              {agentError && <CAlert color="danger" className="py-2">{agentError}</CAlert>}
              {agentSaved && <CAlert color="success" className="py-2">Saved.</CAlert>}
              <div className="mb-3">
                <CFormLabel className="mb-1">Max Turns</CFormLabel>
                <MaxTurnsInput
                  value={agentMaxTurns}
                  onChange={setAgentMaxTurns}
                />
                <small className="text-body-secondary">
                  Current: {task.max_turns === null ? 'Unlimited' : (task.max_turns ?? 50)}
                </small>
              </div>
              <div className="mb-3">
                <CFormLabel className="mb-1">Billing</CFormLabel>
                <CFormSelect
                  value={agentBilling}
                  onChange={(e) => setAgentBilling(e.target.value as ClaudeMode)}
                >
                  <option value="api">API Platform</option>
                  <option value="max">Max (subscription)</option>
                </CFormSelect>
                <small className="text-body-secondary">
                  {agentBilling === 'max'
                    ? 'Uses vendor OAuth / subscription login (Claude Max, ChatGPT Plus, Google OAuth, Qwen OAuth).'
                    : 'Uses the vendor API-key env var from .env.'}
                </small>
              </div>
              <div className="mb-3">
                <CFormLabel className="mb-1">Agent vendor &amp; model</CFormLabel>
                <VendorModelPicker
                  vendor={agentVendor}
                  model={agentModel}
                  onVendorChange={setAgentVendor}
                  onModelChange={setAgentModel}
                  modelLabel="Model"
                  modelPlaceholder="Leave blank to use repo default"
                />
                {task.claude_model && (
                  <small className="text-body-secondary">
                    Current: {task.agent_vendor ?? 'anthropic'} · {task.claude_model}
                  </small>
                )}
              </div>
              <div className="mb-3">
                <CFormLabel className="mb-1">Backup model</CFormLabel>
                <VendorModelPicker
                  vendor={backupVendor}
                  model={backupModel}
                  onVendorChange={setBackupVendor}
                  onModelChange={setBackupModel}
                  modelLabel="Backup Model"
                  modelPlaceholder="Auto-fallback model (default: claude-sonnet-4-6)"
                  vendorName="backup_vendor"
                  modelName="backup_model"
                />
                <small className="text-body-secondary">
                  Auto-failover if primary vendor/model fails after all retries.
                  {task.backup_vendor || task.backup_model
                    ? ` Current: ${task.backup_vendor ? `${task.backup_vendor}/` : ''}${task.backup_model || '(default model)'}`
                    : ''}
                </small>
              </div>
              <div className="mb-3">
                <CFormLabel className="mb-1">Git flow</CFormLabel>
                <CFormSelect
                  value={agentGitFlow}
                  onChange={(e) => setAgentGitFlow(e.target.value as GitFlow)}
                >
                  <option value="branch">Branch + PR</option>
                  <option value="commit">Direct commit</option>
                  <option value="patch">Patch only</option>
                </CFormSelect>
              </div>
              <div className="mb-3">
                <CFormLabel className="mb-1">Verification</CFormLabel>
                <CFormCheck
                  id="skip_verify"
                  label="Skip verification"
                  checked={task.skip_verify}
                  onChange={async (e) => {
                    await fetch(`/api/tasks/${task.id}`, {
                      method: 'PATCH',
                      headers: { 'Content-Type': 'application/json' },
                      body: JSON.stringify({ skip_verify: e.target.checked }),
                    });
                    router.refresh();
                  }}
                />
              </div>
              <CButton
                color="primary"
                size="sm"
                disabled={agentSaving}
                onClick={handleSaveAgent}
              >
                {agentSaving ? 'Saving…' : 'Save'}
              </CButton>
            </CCardBody>
          </CCard>

          {/* Patches (Option A — downloadable format-patch output for manual
              application to a production / mirror repo). */}
          <PatchesPanel taskKey={task.task_key} taskStatus={task.status} />

          {/* Run History */}
          <CCard className="mb-4">
            <CCardHeader><strong>Run History ({runs.length})</strong></CCardHeader>
            <CCardBody className="p-0">
              {runs.length === 0 ? (
                <p className="p-3 mb-0 text-body-secondary">No runs yet.</p>
              ) : (
                <CTable small hover responsive className="mb-0">
                  <CTableHead>
                    <CTableRow>
                      <CTableHeaderCell>Time</CTableHeaderCell>
                      <CTableHeaderCell>Status</CTableHeaderCell>
                      <CTableHeaderCell>Duration</CTableHeaderCell>
                      <CTableHeaderCell>Cost</CTableHeaderCell>
                    </CTableRow>
                  </CTableHead>
                  <CTableBody>
                    {[...runs].sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime()).map((run) => (
                      <CTableRow key={run.id}>
                        <CTableDataCell suppressHydrationWarning>
                          {new Date(run.created_at).toLocaleDateString(undefined, { day: 'numeric', month: 'short' })}{' '}
                          <small className="text-body-secondary">
                            {new Date(run.created_at).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })}
                          </small>
                        </CTableDataCell>
                        <CTableDataCell>
                          <CBadge color={
                            run.status === 'passed' ? 'success' :
                            run.status === 'failed' ? 'danger' :
                            run.status === 'running' ? 'info' : 'secondary'
                          }>
                            {run.status}
                          </CBadge>
                        </CTableDataCell>
                        <CTableDataCell>
                          {run.duration_ms ? `${(run.duration_ms / 1000).toFixed(1)}s` : '-'}
                        </CTableDataCell>
                        <CTableDataCell>
                          {run.cost_usd ? `$${Number(run.cost_usd).toFixed(2)}` : '-'}
                        </CTableDataCell>
                      </CTableRow>
                    ))}
                  </CTableBody>
                </CTable>
              )}
            </CCardBody>
          </CCard>
        </CCol>
      </CRow>

    </>
  );
}
