'use client';

import React, { useState } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';
import {
  CForm,
  CFormLabel,
  CFormInput,
  CFormTextarea,
  CFormSelect,
  CFormCheck,
  CButton,
  CCard,
  CCardBody,
  CCardHeader,
  CRow,
  CCol,
  CAlert,
} from '@coreui/react-pro';
import type { Repo, Task, TaskTemplate, GitFlow, AgentVendor } from '@/lib/types';
import {
  AGENT_VENDORS,
  defaultModelForVendor,
  modelsForVendor,
} from '@/lib/agent-vendors';
import { MaxTurnsInput } from '@/components/MaxTurnsInput';
import { VendorModelPicker } from '@/components/VendorModelPicker';

interface TaskFormProps {
  repos: Repo[];
  task?: Task;
  templates?: TaskTemplate[];
}

export function TaskForm({ repos, task, templates = [] }: TaskFormProps) {
  const router = useRouter();
  const searchParams = useSearchParams();
  const isEdit = !!task;
  const ideaId = !isEdit ? searchParams.get('ideaId') : null;
  const prefillDescription = !isEdit ? searchParams.get('description') : null;

  const [formData, setFormData] = useState({
    repo_id: task?.repo_id?.toString() || '',
    task_key: task?.task_key || '',
    title: task?.title || '',
    description: task?.description || prefillDescription || '',
    acceptance: task?.acceptance || '',
    git_flow: (task?.git_flow || 'branch') as GitFlow,
    claude_mode: task?.claude_mode || 'max',
    agent_vendor: (task?.agent_vendor || 'anthropic') as AgentVendor,
    claude_model: task?.claude_model || '',
    backup_vendor: (task?.backup_vendor || 'anthropic') as AgentVendor,
    backup_model: task?.backup_model || 'claude-sonnet-4-6',
    max_turns: (task?.max_turns ?? 50) as number | null,
    skip_verify: task?.skip_verify ?? false,
  });
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);
  const [filling, setFilling] = useState(false);
  const [fillError, setFillError] = useState('');
  const [refreshing, setRefreshing] = useState(false);
  const [refreshMsg, setRefreshMsg] = useState<{ type: 'success' | 'danger'; text: string } | null>(null);

  const handleApplyTemplate = (templateId: string) => {
    if (!templateId) return;
    const t = templates.find((tpl) => tpl.id === parseInt(templateId));
    if (!t) return;
    setFormData((prev) => ({
      ...prev,
      description: t.description ?? prev.description,
      acceptance: t.acceptance ?? prev.acceptance,
      git_flow: t.git_flow ?? prev.git_flow,
      claude_mode: t.claude_mode ?? prev.claude_mode,
      agent_vendor: (t.agent_vendor ?? prev.agent_vendor) as AgentVendor,
      claude_model: t.claude_model ?? prev.claude_model,
      backup_vendor: (t.backup_vendor ?? t.agent_vendor ?? prev.backup_vendor) as AgentVendor,
      backup_model: t.backup_model ?? prev.backup_model,
      max_turns: t.max_turns ?? prev.max_turns,
      skip_verify: t.skip_verify ?? prev.skip_verify,
    }));
  };

  const handleChange = (
    e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>,
  ) => {
    setFormData((prev) => ({ ...prev, [e.target.name]: e.target.value }));
  };

  const handleCheck = (e: React.ChangeEvent<HTMLInputElement>) => {
    setFormData((prev) => ({ ...prev, [e.target.name]: e.target.checked }));
  };

  // Swapping the vendor combobox auto-resets the model to the vendor's
  // default unless the current model string already belongs to the new
  // vendor's suggested list (i.e. the user typed something valid for both).
  const handleVendorChange = (next: AgentVendor) => {
    setFormData((prev) => {
      const belongsToNextVendor = modelsForVendor(next).some(
        (m) => m.id === prev.claude_model,
      );
      return {
        ...prev,
        agent_vendor: next,
        claude_model: belongsToNextVendor
          ? prev.claude_model
          : defaultModelForVendor(next),
      };
    });
  };

  const handleFillTask = async () => {
    if (!formData.description.trim()) return;
    setFilling(true);
    setFillError('');
    try {
      const res = await fetch('/api/tasks/generate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ description: formData.description }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => null);
        throw new Error(data?.error || 'devtask skill failed');
      }
      const task = await res.json();
      setFormData((prev) => ({
        ...prev,
        task_key: task.task_key ?? prev.task_key,
        title: task.title ?? prev.title,
        description: task.description ?? prev.description,
        acceptance: task.acceptance ?? prev.acceptance,
        git_flow: task.git_flow ?? prev.git_flow,
        claude_mode: task.claude_mode ?? prev.claude_mode,
        agent_vendor: task.agent_vendor ?? prev.agent_vendor,
        claude_model: task.claude_model ?? prev.claude_model,
        max_turns: task.max_turns ?? prev.max_turns,
        skip_verify: task.skip_verify ?? prev.skip_verify,
      }));
    } catch (err) {
      setFillError(err instanceof Error ? err.message : 'devtask skill failed');
    } finally {
      setFilling(false);
    }
  };

  const handleRefreshGit = async () => {
    if (!formData.repo_id) return;
    setRefreshing(true);
    setRefreshMsg(null);
    try {
      const res = await fetch(`/api/repos/${formData.repo_id}/refresh-git`, {
        method: 'POST',
      });
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
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSaving(true);
    setError('');

    try {
      const body = {
        ...formData,
        repo_id: parseInt(formData.repo_id),
        priority: 3,
        labels: [],
        mode: 'autonomous',
        claude_model: formData.claude_model.trim() || null,
        backup_vendor: formData.backup_vendor !== formData.agent_vendor ? formData.backup_vendor : null,
        backup_model: formData.backup_model.trim() || null,
        max_turns: formData.max_turns,
      };

      const url = isEdit ? `/api/tasks/${task!.id}` : '/api/tasks';
      const method = isEdit ? 'PATCH' : 'POST';

      const res = await fetch(url, {
        method,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });

      if (!res.ok) {
        const data = await res.json();
        throw new Error(data.error || 'Failed to save task');
      }

      const data = await res.json();

      if (!isEdit && ideaId) {
        await fetch(`/api/ideas/${ideaId}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ tasked: true, task_id: data.id }),
        }).catch((e) => console.error('Failed to mark idea tasked:', e));
      }

      router.push(`/tasks/${data.id}`);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'An error occurred');
    } finally {
      setSaving(false);
    }
  };

  return (
    <CCard>
      <CCardHeader>
        <strong>{isEdit ? 'Edit Task' : 'Create Task'}</strong>
      </CCardHeader>
      <CCardBody>
        {error && <CAlert color="danger">{error}</CAlert>}
        <CForm onSubmit={handleSubmit}>
          {!isEdit && templates.length > 0 && (
            <div className="mb-3">
              <CFormLabel>Load from Template</CFormLabel>
              <CFormSelect
                onChange={(e) => handleApplyTemplate(e.target.value)}
                defaultValue=""
              >
                <option value="">-- Select a template --</option>
                {templates.map((t) => (
                  <option key={t.id} value={t.id}>
                    {t.name}
                  </option>
                ))}
              </CFormSelect>
              <small className="text-body-secondary">Pre-fills description, acceptance, and agent settings from a saved template.</small>
            </div>
          )}

          <CRow className="mb-3">
            <CCol md={6}>
              <div className="d-flex align-items-center gap-2 mb-1">
                <CFormLabel className="mb-0">Repository</CFormLabel>
                <CButton
                  type="button"
                  color="info"
                  variant="outline"
                  size="sm"
                  disabled={refreshing || !formData.repo_id}
                  onClick={handleRefreshGit}
                >
                  {refreshing ? 'Refreshing...' : 'Refresh Git'}
                </CButton>
                {refreshMsg && (
                  <span className={`small text-${refreshMsg.type}`}>{refreshMsg.text}</span>
                )}
              </div>
              <CFormSelect
                name="repo_id"
                value={formData.repo_id}
                onChange={handleChange}
                required
              >
                <option value="">Select a repository...</option>
                {repos.map((repo) => (
                  <option key={repo.id} value={repo.id}>
                    {repo.name}
                  </option>
                ))}
              </CFormSelect>
            </CCol>
            <CCol md={6}>
              <CFormLabel>Task Key</CFormLabel>
              <CFormInput
                name="task_key"
                value={formData.task_key}
                onChange={handleChange}
                placeholder="e.g. FIX-auth-login (no spaces)"
                required
              />
            </CCol>
          </CRow>

          <div className="mb-3">
            <CFormLabel>Title</CFormLabel>
            <CFormInput
              name="title"
              value={formData.title}
              onChange={handleChange}
              placeholder="Brief task description"
              required
            />
          </div>

          <div className="mb-3">
            <div className="d-flex align-items-center gap-2 mb-1">
              <CFormLabel className="mb-0">Description</CFormLabel>
              <CButton
                type="button"
                color="info"
                variant="outline"
                size="sm"
                disabled={filling || !formData.description.trim()}
                onClick={handleFillTask}
              >
                {filling ? 'Filling...' : 'Fill Task'}
              </CButton>
              {fillError && (
                <span className="text-danger small">{fillError}</span>
              )}
            </div>
            <CFormTextarea
              name="description"
              value={formData.description}
              onChange={handleChange}
              rows={4}
              placeholder="Detailed task description..."
            />
          </div>

          <div className="mb-3">
            <CFormLabel>Acceptance Criteria</CFormLabel>
            <CFormTextarea
              name="acceptance"
              value={formData.acceptance}
              onChange={handleChange}
              rows={3}
              placeholder="What conditions must be met for this task to be considered done?"
            />
          </div>

          <div className="mb-3">
            <CFormLabel>Git flow</CFormLabel>
            <div className="btn-group w-100" role="group">
              {(
                [
                  { value: 'branch', label: 'Branch + PR',     title: 'Create agent/… branch and open a pull request (default)' },
                  { value: 'commit', label: 'Direct commit',   title: 'Squash-merge directly onto the default branch — no PR' },
                  { value: 'patch',  label: 'Patch only',      title: 'Generate a combined.mbox patch file — no push, no PR' },
                ] as { value: GitFlow; label: string; title: string }[]
              ).map(({ value, label, title }) => (
                <React.Fragment key={value}>
                  <input
                    type="radio"
                    className="btn-check"
                    name="git_flow"
                    id={`git_flow_${value}`}
                    value={value}
                    checked={formData.git_flow === value}
                    onChange={handleChange}
                    autoComplete="off"
                  />
                  <label
                    className={`btn btn-outline-secondary`}
                    htmlFor={`git_flow_${value}`}
                    title={title}
                  >
                    {label}
                  </label>
                </React.Fragment>
              ))}
            </div>
          </div>

          <CRow className="mb-3">
            <CCol md={3}>
              <CFormLabel>Max Turns</CFormLabel>
              <MaxTurnsInput
                value={formData.max_turns}
                onChange={(v) => setFormData((prev) => ({ ...prev, max_turns: v }))}
              />
              <small className="text-body-secondary">Blank or empty = unlimited</small>
            </CCol>
            <CCol md={3}>
              <CFormLabel>Billing</CFormLabel>
              <CFormSelect
                name="claude_mode"
                value={formData.claude_mode}
                onChange={handleChange}
              >
                <option value="api">API Platform</option>
                <option value="max">Max (subscription)</option>
              </CFormSelect>
            </CCol>
          </CRow>

          <CRow className="mb-3">
            <CCol md={3}>
              <CFormLabel>Vendor</CFormLabel>
              <CFormSelect
                name="agent_vendor"
                value={formData.agent_vendor}
                onChange={(e) => handleVendorChange(e.target.value as AgentVendor)}
              >
                {AGENT_VENDORS.map((v) => (
                  <option key={v.id} value={v.id}>
                    {v.label}
                  </option>
                ))}
              </CFormSelect>
            </CCol>
            <CCol md={6}>
              <CFormLabel>
                Model{' '}
                <small className="text-body-secondary">
                  (blank = repo default
                  {formData.repo_id
                    ? `: ${repos.find((r) => r.id === parseInt(formData.repo_id))?.claude_model ?? '—'}`
                    : ''}
                  )
                </small>
              </CFormLabel>
              <CFormInput
                name="claude_model"
                value={formData.claude_model}
                onChange={(e) =>
                  setFormData((prev) => ({ ...prev, claude_model: e.target.value }))
                }
                list={`task-model-datalist-${formData.agent_vendor}`}
                placeholder="Leave blank to use repo default"
                autoComplete="off"
              />
              <datalist id={`task-model-datalist-${formData.agent_vendor}`}>
                {modelsForVendor(formData.agent_vendor).map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.label}
                  </option>
                ))}
              </datalist>
            </CCol>
          </CRow>

          <div className="mb-3">
            <CFormLabel>Backup Model</CFormLabel>
            <VendorModelPicker
              vendor={formData.backup_vendor}
              model={formData.backup_model}
              onVendorChange={(v) => setFormData((prev) => ({
                ...prev,
                backup_vendor: v,
                backup_model: modelsForVendor(v).some((m) => m.id === prev.backup_model)
                  ? prev.backup_model
                  : defaultModelForVendor(v),
              }))}
              onModelChange={(m) => setFormData((prev) => ({ ...prev, backup_model: m }))}
              modelLabel="Backup Model"
              modelPlaceholder="Auto-fallback model (default: claude-sonnet-4-6)"
              vendorName="backup_vendor"
              modelName="backup_model"
            />
            <small className="text-body-secondary">Auto-failover: if the primary vendor/model fails after all retries, switches to this backup. Different vendor = cross-vendor failover.</small>
          </div>

          <div className="mb-3">
            <CFormCheck
              name="skip_verify"
              id="skip_verify"
              label="Skip verification (no build/test/lint — go straight to PR)"
              checked={formData.skip_verify}
              onChange={handleCheck}
            />
          </div>

          <div className="d-flex gap-2">
            <CButton type="submit" color="primary" disabled={saving}>
              {saving ? 'Saving...' : isEdit ? 'Update Task' : 'Create Task'}
            </CButton>
            <CButton
              type="button"
              color="secondary"
              variant="outline"
              onClick={() => router.back()}
            >
              Cancel
            </CButton>
          </div>
        </CForm>
      </CCardBody>
    </CCard>
  );
}
