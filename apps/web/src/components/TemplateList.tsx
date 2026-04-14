'use client';

import React, { useCallback, useState } from 'react';
import { useRouter } from 'next/navigation';
import {
  CCard,
  CCardBody,
  CCardHeader,
  CButton,
  CTable,
  CTableHead,
  CTableRow,
  CTableHeaderCell,
  CTableBody,
  CTableDataCell,
  CBadge,
  CModal,
  CModalHeader,
  CModalTitle,
  CModalBody,
  CModalFooter,
  CFormLabel,
  CFormInput,
  CFormTextarea,
  CFormSelect,
  CFormCheck,
  CAlert,
  CRow,
  CCol,
} from '@coreui/react-pro';
import type { TaskTemplate, GitFlow, ClaudeMode, AgentVendor } from '@/lib/types';
import { AGENT_VENDORS, defaultModelForVendor, modelsForVendor } from '@/lib/agent-vendors';
import { VendorModelPicker } from './VendorModelPicker';
import { MaxTurnsInput } from './MaxTurnsInput';

interface TemplateListProps {
  templates: TaskTemplate[];
}

const EMPTY_FORM = {
  name: '',
  description: '',
  acceptance: '',
  git_flow: 'branch' as GitFlow,
  claude_mode: 'max' as ClaudeMode,
  agent_vendor: 'anthropic' as AgentVendor,
  claude_model: '',
  backup_vendor: 'anthropic' as AgentVendor,
  backup_model: 'claude-sonnet-4-6',
  max_turns: 50 as number | null,
  skip_verify: false,
};

type FormData = typeof EMPTY_FORM;

function templateToForm(t: TaskTemplate): FormData {
  return {
    name: t.name,
    description: t.description ?? '',
    acceptance: t.acceptance ?? '',
    git_flow: t.git_flow,
    claude_mode: t.claude_mode,
    agent_vendor: t.agent_vendor,
    claude_model: t.claude_model ?? '',
    backup_vendor: (t.backup_vendor ?? t.agent_vendor) as AgentVendor,
    backup_model: t.backup_model ?? 'claude-sonnet-4-6',
    max_turns: t.max_turns ?? 50,
    skip_verify: t.skip_verify,
  };
}

export function TemplateList({ templates }: TemplateListProps) {
  const router = useRouter();
  const [modalVisible, setModalVisible] = useState(false);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [form, setForm] = useState<FormData>({ ...EMPTY_FORM });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  const openCreate = useCallback(() => {
    setEditingId(null);
    setForm({ ...EMPTY_FORM });
    setError('');
    setModalVisible(true);
  }, []);

  const openEdit = useCallback((t: TaskTemplate) => {
    setEditingId(t.id);
    setForm(templateToForm(t));
    setError('');
    setModalVisible(true);
  }, []);

  const handleDelete = useCallback(async (t: TaskTemplate) => {
    if (!confirm(`Delete template "${t.name}"?`)) return;
    await fetch(`/api/templates/${t.id}`, { method: 'DELETE' });
    router.refresh();
  }, [router]);

  const handleSave = useCallback(async () => {
    if (!form.name.trim()) {
      setError('Name is required');
      return;
    }
    setSaving(true);
    setError('');
    try {
      const body = {
        name: form.name.trim(),
        description: form.description.trim() || null,
        acceptance: form.acceptance.trim() || null,
        git_flow: form.git_flow,
        claude_mode: form.claude_mode,
        agent_vendor: form.agent_vendor,
        claude_model: form.claude_model.trim() || null,
        backup_vendor: form.backup_vendor !== form.agent_vendor ? form.backup_vendor : null,
        backup_model: form.backup_model.trim() || null,
        max_turns: form.max_turns,
        skip_verify: form.skip_verify,
      };

      const url = editingId ? `/api/templates/${editingId}` : '/api/templates';
      const method = editingId ? 'PATCH' : 'POST';
      const res = await fetch(url, {
        method,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        throw new Error(d.error || 'Save failed');
      }
      setModalVisible(false);
      router.refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Save failed');
    } finally {
      setSaving(false);
    }
  }, [form, editingId, router]);

  const handleChange = (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>) => {
    setForm((prev) => ({ ...prev, [e.target.name]: e.target.value }));
  };

  const GIT_FLOW_LABELS: Record<string, string> = { branch: 'Branch + PR', commit: 'Direct commit', patch: 'Patch only' };
  const BILLING_LABELS: Record<string, string> = { max: 'Max', api: 'API' };

  return (
    <>
      <CCard>
        <CCardHeader className="d-flex justify-content-between align-items-center">
          <strong>Templates ({templates.length})</strong>
          <CButton color="primary" size="sm" onClick={openCreate}>
            + New Template
          </CButton>
        </CCardHeader>
        <CCardBody className="p-0">
          {templates.length === 0 ? (
            <div className="p-4 text-center text-body-secondary">
              <p className="mb-2">No templates yet.</p>
              <p className="mb-0">Templates let you create tasks faster with pre-filled settings.</p>
            </div>
          ) : (
            <CTable hover responsive className="mb-0">
              <CTableHead>
                <CTableRow>
                  <CTableHeaderCell>Name</CTableHeaderCell>
                  <CTableHeaderCell>Git Flow</CTableHeaderCell>
                  <CTableHeaderCell>Vendor / Model</CTableHeaderCell>
                  <CTableHeaderCell>Billing</CTableHeaderCell>
                  <CTableHeaderCell>Turns</CTableHeaderCell>
                  <CTableHeaderCell style={{ width: 120 }}>Actions</CTableHeaderCell>
                </CTableRow>
              </CTableHead>
              <CTableBody>
                {templates.map((t) => (
                  <CTableRow key={t.id}>
                    <CTableDataCell>
                      <strong>{t.name}</strong>
                      {t.description && (
                        <div className="text-body-secondary small text-truncate" style={{ maxWidth: 300 }}>
                          {t.description}
                        </div>
                      )}
                    </CTableDataCell>
                    <CTableDataCell>
                      <CBadge color="secondary">{GIT_FLOW_LABELS[t.git_flow] ?? t.git_flow}</CBadge>
                    </CTableDataCell>
                    <CTableDataCell>
                      <small>{t.agent_vendor}</small>
                      {t.claude_model && <> / <code>{t.claude_model}</code></>}
                    </CTableDataCell>
                    <CTableDataCell>{BILLING_LABELS[t.claude_mode] ?? t.claude_mode}</CTableDataCell>
                    <CTableDataCell>{t.max_turns ?? 'Unlimited'}</CTableDataCell>
                    <CTableDataCell>
                      <div className="d-flex gap-1">
                        <CButton color="primary" variant="outline" size="sm" onClick={() => openEdit(t)}>
                          Edit
                        </CButton>
                        <CButton color="danger" variant="outline" size="sm" onClick={() => handleDelete(t)}>
                          Del
                        </CButton>
                      </div>
                    </CTableDataCell>
                  </CTableRow>
                ))}
              </CTableBody>
            </CTable>
          )}
        </CCardBody>
      </CCard>

      {/* Create / Edit modal */}
      <CModal visible={modalVisible} onClose={() => setModalVisible(false)} size="lg">
        <CModalHeader>
          <CModalTitle>{editingId ? 'Edit Template' : 'New Template'}</CModalTitle>
        </CModalHeader>
        <CModalBody>
          {error && <CAlert color="danger">{error}</CAlert>}

          <div className="mb-3">
            <CFormLabel>Template Name</CFormLabel>
            <CFormInput
              name="name"
              value={form.name}
              onChange={handleChange}
              placeholder='e.g. "Fix lint errors", "Add unit tests"'
              required
            />
          </div>

          <div className="mb-3">
            <CFormLabel>Description</CFormLabel>
            <CFormTextarea
              name="description"
              value={form.description}
              onChange={handleChange}
              rows={3}
              placeholder="Pre-filled task description..."
            />
          </div>

          <div className="mb-3">
            <CFormLabel>Acceptance Criteria</CFormLabel>
            <CFormTextarea
              name="acceptance"
              value={form.acceptance}
              onChange={handleChange}
              rows={2}
              placeholder="Pre-filled acceptance criteria..."
            />
          </div>

          <CRow className="mb-3">
            <CCol md={4}>
              <CFormLabel>Git Flow</CFormLabel>
              <CFormSelect name="git_flow" value={form.git_flow} onChange={handleChange}>
                <option value="branch">Branch + PR</option>
                <option value="commit">Direct commit</option>
                <option value="patch">Patch only</option>
              </CFormSelect>
            </CCol>
            <CCol md={4}>
              <CFormLabel>Billing</CFormLabel>
              <CFormSelect name="claude_mode" value={form.claude_mode} onChange={handleChange}>
                <option value="api">API Platform</option>
                <option value="max">Max (subscription)</option>
              </CFormSelect>
            </CCol>
            <CCol md={4}>
              <CFormLabel>Max Turns</CFormLabel>
              <MaxTurnsInput
                value={form.max_turns}
                onChange={(v) => setForm((prev) => ({ ...prev, max_turns: v }))}
              />
            </CCol>
          </CRow>

          <div className="mb-3">
            <CFormLabel>Primary Vendor &amp; Model</CFormLabel>
            <VendorModelPicker
              vendor={form.agent_vendor}
              model={form.claude_model}
              onVendorChange={(v) => {
                const belongsToNext = modelsForVendor(v).some((m) => m.id === form.claude_model);
                setForm((prev) => ({
                  ...prev,
                  agent_vendor: v,
                  claude_model: belongsToNext ? prev.claude_model : defaultModelForVendor(v),
                }));
              }}
              onModelChange={(m) => setForm((prev) => ({ ...prev, claude_model: m }))}
            />
          </div>

          <div className="mb-3">
            <CFormLabel>Backup Vendor &amp; Model</CFormLabel>
            <VendorModelPicker
              vendor={form.backup_vendor}
              model={form.backup_model}
              onVendorChange={(v) => {
                const belongsToNext = modelsForVendor(v).some((m) => m.id === form.backup_model);
                setForm((prev) => ({
                  ...prev,
                  backup_vendor: v,
                  backup_model: belongsToNext ? prev.backup_model : defaultModelForVendor(v),
                }));
              }}
              onModelChange={(m) => setForm((prev) => ({ ...prev, backup_model: m }))}
              modelLabel="Backup Model"
              modelPlaceholder="Auto-fallover model"
              vendorName="backup_vendor"
              modelName="backup_model"
            />
          </div>

          <div className="mb-3">
            <CFormCheck
              id="template_skip_verify"
              name="skip_verify"
              label="Skip verification"
              checked={form.skip_verify}
              onChange={(e) => setForm((prev) => ({ ...prev, skip_verify: e.target.checked }))}
            />
          </div>
        </CModalBody>
        <CModalFooter>
          <CButton color="secondary" variant="outline" onClick={() => setModalVisible(false)}>
            Cancel
          </CButton>
          <CButton color="primary" disabled={saving} onClick={handleSave}>
            {saving ? 'Saving...' : editingId ? 'Update' : 'Create'}
          </CButton>
        </CModalFooter>
      </CModal>
    </>
  );
}
