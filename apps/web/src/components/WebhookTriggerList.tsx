'use client';

import React, { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import {
  CAlert,
  CBadge,
  CButton,
  CCard,
  CCardBody,
  CCardHeader,
  CCol,
  CFormInput,
  CFormLabel,
  CFormSelect,
  CFormSwitch,
  CFormTextarea,
  CModal,
  CModalBody,
  CModalFooter,
  CModalHeader,
  CModalTitle,
  CRow,
  CTable,
  CTableBody,
  CTableDataCell,
  CTableHead,
  CTableHeaderCell,
  CTableRow,
} from '@coreui/react-pro';
import type { PublicWebhookTrigger } from '@/lib/webhooks/triggers';
import { PRIORITY_LABELS, type TaskPriority } from '@/lib/types';

interface NamedRow {
  id: number;
  name: string;
}

interface WebhookTriggerListProps {
  triggers: PublicWebhookTrigger[];
  repos: NamedRow[];
  templates: NamedRow[];
}

const EMPTY_FORM = {
  name: '',
  repo_id: '',
  template_id: '',
  secret: '',
  title_template: 'Webhook: {{name}}',
  description_template: '',
  task_key_prefix: 'HOOK',
  priority: '3',
};

type TriggerForm = typeof EMPTY_FORM;

/** The public URL an external system should POST to. */
function triggerUrl(origin: string, token: string): string {
  return `${origin}/api/webhooks/${token}`;
}

/**
 * A copy-pasteable shell example. The secret is write-only (never sent back
 * by the API), so it appears as a variable the operator fills in.
 */
function curlExample(url: string): string {
  return [
    `SECRET='<the secret you set for this trigger>'`,
    `BODY='{"title":"Example","details":{"severity":"high"}}'`,
    `SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | sed 's/^.* //')`,
    `curl -X POST '${url}' \\`,
    `  -H 'Content-Type: application/json' \\`,
    `  -H "X-DevServer-Signature: sha256=$SIG" \\`,
    `  -d "$BODY"`,
  ].join('\n');
}

/**
 * Admin view of webhook triggers: each one maps a secret URL to a repository
 * and queues a task whenever an external system calls it with a valid
 * signature.
 */
export function WebhookTriggerList({ triggers, repos, templates }: WebhookTriggerListProps) {
  const router = useRouter();
  const [modalVisible, setModalVisible] = useState(false);
  const [form, setForm] = useState<TriggerForm>({ ...EMPTY_FORM });
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState('');
  const [listError, setListError] = useState('');
  const [expandedId, setExpandedId] = useState<number | null>(null);
  // Read after mount so server and client render the same markup.
  const [origin, setOrigin] = useState('');

  useEffect(() => {
    setOrigin(window.location.origin);
  }, []);

  const openCreate = () => {
    setForm({ ...EMPTY_FORM, repo_id: repos[0] ? String(repos[0].id) : '' });
    setFormError('');
    setModalVisible(true);
  };

  const handleChange = (
    e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>,
  ) => {
    setForm((prev) => ({ ...prev, [e.target.name]: e.target.value }));
  };

  const handleCreate = async () => {
    if (!form.secret.trim()) {
      setFormError('A secret is required — callers sign each request with it.');
      return;
    }
    setSaving(true);
    setFormError('');
    try {
      const res = await fetch('/api/webhook-triggers', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: form.name,
          repo_id: Number(form.repo_id),
          template_id: form.template_id ? Number(form.template_id) : null,
          secret: form.secret,
          title_template: form.title_template,
          description_template: form.description_template,
          task_key_prefix: form.task_key_prefix,
          priority: Number(form.priority),
        }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.error || 'Could not create the trigger');
      }
      setModalVisible(false);
      router.refresh();
    } catch (err) {
      setFormError(err instanceof Error ? err.message : 'Could not create the trigger');
    } finally {
      setSaving(false);
    }
  };

  const setEnabled = async (trigger: PublicWebhookTrigger, enabled: boolean) => {
    setListError('');
    const res = await fetch(`/api/webhook-triggers/${trigger.id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled }),
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      setListError(data.error || 'Could not update the trigger');
    }
    router.refresh();
  };

  const handleDelete = async (trigger: PublicWebhookTrigger) => {
    if (!confirm(`Delete webhook "${trigger.name}"? Callers using its URL will get 404.`)) return;
    setListError('');
    const res = await fetch(`/api/webhook-triggers/${trigger.id}`, { method: 'DELETE' });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      setListError(data.error || 'Could not delete the trigger');
    }
    router.refresh();
  };

  return (
    <>
      <CCard>
        <CCardHeader className="d-flex justify-content-between align-items-center">
          <strong>Triggers ({triggers.length})</strong>
          <CButton color="primary" size="sm" onClick={openCreate} disabled={repos.length === 0}>
            + New Webhook
          </CButton>
        </CCardHeader>
        <CCardBody className="p-0">
          {listError && <CAlert color="danger" className="m-3 py-2">{listError}</CAlert>}
          {triggers.length === 0 ? (
            <div className="p-4 text-center text-body-secondary">
              <p className="mb-2">No webhooks yet.</p>
              <p className="mb-0">
                A webhook gives an external system (CI, an issue tracker, a chat bot) a
                signed URL that queues a task on one of your repositories.
              </p>
            </div>
          ) : (
            <CTable hover responsive className="mb-0">
              <CTableHead>
                <CTableRow>
                  <CTableHeaderCell>Name</CTableHeaderCell>
                  <CTableHeaderCell>Repository</CTableHeaderCell>
                  <CTableHeaderCell>Key prefix</CTableHeaderCell>
                  <CTableHeaderCell>Fired</CTableHeaderCell>
                  <CTableHeaderCell>Enabled</CTableHeaderCell>
                  <CTableHeaderCell style={{ width: 180 }}>Actions</CTableHeaderCell>
                </CTableRow>
              </CTableHead>
              <CTableBody>
                {triggers.map((trigger) => (
                  <React.Fragment key={trigger.id}>
                    <CTableRow>
                      <CTableDataCell>
                        <strong>{trigger.name}</strong>
                        {!trigger.has_secret && (
                          <CBadge color="danger" className="ms-2" title="Unsigned triggers never fire">
                            no secret
                          </CBadge>
                        )}
                      </CTableDataCell>
                      <CTableDataCell>{trigger.repo_name ?? `#${trigger.repo_id}`}</CTableDataCell>
                      <CTableDataCell><code>{trigger.task_key_prefix ?? 'HOOK'}</code></CTableDataCell>
                      <CTableDataCell suppressHydrationWarning>
                        {trigger.fire_count}×
                        {trigger.last_fired_at && (
                          <div className="small text-body-secondary">
                            last {new Date(trigger.last_fired_at).toLocaleString()}
                          </div>
                        )}
                      </CTableDataCell>
                      <CTableDataCell>
                        <CFormSwitch
                          checked={trigger.enabled}
                          onChange={(e) => setEnabled(trigger, e.target.checked)}
                          aria-label={`Enable ${trigger.name}`}
                        />
                      </CTableDataCell>
                      <CTableDataCell>
                        <div className="d-flex gap-1">
                          <CButton
                            color="secondary"
                            variant="outline"
                            size="sm"
                            onClick={() => setExpandedId(expandedId === trigger.id ? null : trigger.id)}
                          >
                            {expandedId === trigger.id ? 'Hide usage' : 'Usage'}
                          </CButton>
                          <CButton color="danger" variant="outline" size="sm" onClick={() => handleDelete(trigger)}>
                            Delete
                          </CButton>
                        </div>
                      </CTableDataCell>
                    </CTableRow>
                    {expandedId === trigger.id && (
                      <CTableRow>
                        <CTableDataCell colSpan={6} className="bg-body-tertiary">
                          <div className="small mb-1 fw-semibold">URL</div>
                          <code className="d-block mb-2 text-break">{triggerUrl(origin, trigger.token)}</code>
                          <div className="small mb-1 fw-semibold">Example call</div>
                          <pre className="small mb-1 p-2 border rounded bg-body">
                            {curlExample(triggerUrl(origin, trigger.token))}
                          </pre>
                          <div className="small text-body-secondary">
                            The JSON body fills the trigger&apos;s templates: <code>{'{{title}}'}</code>,{' '}
                            <code>{'{{details.severity}}'}</code>, <code>{'{{items.0.name}}'}</code>; and{' '}
                            <code>{'{{name}}'}</code> is the trigger&apos;s name.
                          </div>
                        </CTableDataCell>
                      </CTableRow>
                    )}
                  </React.Fragment>
                ))}
              </CTableBody>
            </CTable>
          )}
        </CCardBody>
      </CCard>

      <CModal visible={modalVisible} onClose={() => setModalVisible(false)} size="lg">
        <CModalHeader>
          <CModalTitle>New Webhook</CModalTitle>
        </CModalHeader>
        <CModalBody>
          {formError && <CAlert color="danger">{formError}</CAlert>}
          <CRow className="mb-3">
            <CCol md={6}>
              <CFormLabel>Name</CFormLabel>
              <CFormInput name="name" value={form.name} onChange={handleChange} placeholder="CI failures" />
            </CCol>
            <CCol md={6}>
              <CFormLabel>Repository</CFormLabel>
              <CFormSelect name="repo_id" value={form.repo_id} onChange={handleChange}>
                {repos.map((repo) => (
                  <option key={repo.id} value={repo.id}>{repo.name}</option>
                ))}
              </CFormSelect>
            </CCol>
          </CRow>
          <CRow className="mb-3">
            <CCol md={6}>
              <CFormLabel>Secret</CFormLabel>
              <CFormInput
                name="secret"
                type="password"
                value={form.secret}
                onChange={handleChange}
                autoComplete="new-password"
              />
              <small className="text-body-secondary">
                Required. Callers sign each body with it; it is never shown again.
              </small>
            </CCol>
            <CCol md={6}>
              <CFormLabel>Task template (optional)</CFormLabel>
              <CFormSelect name="template_id" value={form.template_id} onChange={handleChange}>
                <option value="">None — default agent settings</option>
                {templates.map((template) => (
                  <option key={template.id} value={template.id}>{template.name}</option>
                ))}
              </CFormSelect>
            </CCol>
          </CRow>
          <div className="mb-3">
            <CFormLabel>Title template</CFormLabel>
            <CFormInput name="title_template" value={form.title_template} onChange={handleChange} />
          </div>
          <div className="mb-3">
            <CFormLabel>Description template</CFormLabel>
            <CFormTextarea
              name="description_template"
              value={form.description_template}
              onChange={handleChange}
              rows={3}
              placeholder="{{details.summary}}"
            />
            <small className="text-body-secondary">
              <code>{'{{path.to.field}}'}</code> is replaced with that field of the JSON body;
              missing fields become empty.
            </small>
          </div>
          <CRow>
            <CCol md={6}>
              <CFormLabel>Task key prefix</CFormLabel>
              <CFormInput name="task_key_prefix" value={form.task_key_prefix} onChange={handleChange} />
            </CCol>
            <CCol md={6}>
              <CFormLabel>Priority</CFormLabel>
              <CFormSelect name="priority" value={form.priority} onChange={handleChange}>
                {([1, 2, 3, 4] as TaskPriority[]).map((priority) => (
                  <option key={priority} value={priority}>{PRIORITY_LABELS[priority]}</option>
                ))}
              </CFormSelect>
            </CCol>
          </CRow>
        </CModalBody>
        <CModalFooter>
          <CButton color="secondary" variant="outline" onClick={() => setModalVisible(false)}>
            Cancel
          </CButton>
          <CButton color="primary" disabled={saving} onClick={handleCreate}>
            {saving ? 'Creating…' : 'Create'}
          </CButton>
        </CModalFooter>
      </CModal>
    </>
  );
}
