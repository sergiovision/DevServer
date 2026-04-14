'use client';

import React, { useCallback, useState } from 'react';
import {
  CCard,
  CCardBody,
  CCardHeader,
  CForm,
  CFormLabel,
  CFormInput,
  CFormSwitch,
  CFormSelect,
  CButton,
  CAlert,
  CRow,
  CCol,
} from '@coreui/react-pro';
import type { AgentVendor } from '@/lib/types';
import {
  AGENT_VENDORS,
  defaultModelForVendor,
  modelsForVendor,
} from '@/lib/agent-vendors';
interface SettingsFormProps {
  settings: Record<string, unknown>;
}

/** Settings values arrive as JSON — strip the wrapping quotes if any. */
function unquote(val: unknown): string {
  if (typeof val === 'string') {
    const trimmed = val.trim();
    if (trimmed.startsWith('"') && trimmed.endsWith('"')) {
      return trimmed.slice(1, -1);
    }
    return trimmed;
  }
  return String(val ?? '');
}

/** Build a clean draft object from the raw settings record. */
function buildDraft(raw: Record<string, unknown>) {
  return {
    max_concurrency: Number(raw.max_concurrency || 2),
    queue_paused: Boolean(raw.queue_paused),
    auto_enqueue: Boolean(raw.auto_enqueue),
    notifications_enabled: raw.notifications_enabled !== false,
    system_llm_vendor: (unquote(raw.system_llm_vendor) || 'glm') as AgentVendor,
    system_llm_model: unquote(raw.system_llm_model) || 'glm-5.1',
  };
}

export function SettingsForm({ settings: initial }: SettingsFormProps) {
  const [draft, setDraft] = useState(() => buildDraft(initial));
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState('');

  // ── Local-only state changes (no API call) ─────────────────────────
  const set = useCallback(
    <K extends keyof typeof draft>(key: K, value: (typeof draft)[K]) => {
      setSaved(false);
      setDraft((prev) => ({ ...prev, [key]: value }));
    },
    [],
  );

  // Vendor change auto-resets model if the current model doesn't belong
  // to the new vendor's suggested list.
  const handleVendorChange = useCallback(
    (next: AgentVendor) => {
      setDraft((prev) => {
        const belongs = modelsForVendor(next).some((m) => m.id === prev.system_llm_model);
        return {
          ...prev,
          system_llm_vendor: next,
          system_llm_model: belongs ? prev.system_llm_model : defaultModelForVendor(next),
        };
      });
      setSaved(false);
    },
    [],
  );

  // ── Save all settings at once ──────────────────────────────────────
  const handleSave = useCallback(async () => {
    setSaving(true);
    setSaved(false);
    setError('');
    try {
      const pairs: [string, unknown][] = [
        ['max_concurrency', draft.max_concurrency],
        ['queue_paused', draft.queue_paused],
        ['auto_enqueue', draft.auto_enqueue],
        ['notifications_enabled', draft.notifications_enabled],
        ['system_llm_vendor', draft.system_llm_vendor],
        ['system_llm_model', draft.system_llm_model],
      ];
      await Promise.all(
        pairs.map(([key, value]) =>
          fetch('/api/settings', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ key, value }),
          }),
        ),
      );
      setSaved(true);
      setTimeout(() => setSaved(false), 3000);
    } catch {
      setError('Failed to save settings');
    } finally {
      setSaving(false);
    }
  }, [draft]);

  // Model datalist for the selected system LLM vendor
  const sysModelList = modelsForVendor(draft.system_llm_vendor);

  return (
    <>
      {/* General Settings */}
      <CCard className="mb-4">
        <CCardHeader><strong>General</strong></CCardHeader>
        <CCardBody>
          {error && <CAlert color="danger" className="py-2">{error}</CAlert>}
          {saved && <CAlert color="success" className="py-2">Settings saved.</CAlert>}
          <CForm onSubmit={(e) => { e.preventDefault(); handleSave(); }}>
            <CRow className="mb-3">
              <CCol md={4}>
                <CFormLabel>Max Concurrency</CFormLabel>
                <CFormInput
                  type="number"
                  min="1"
                  max="10"
                  value={String(draft.max_concurrency)}
                  onChange={(e) => set('max_concurrency', parseInt(e.target.value) || 1)}
                />
              </CCol>
            </CRow>

            <CRow className="mb-3">
              <CCol md={4}>
                <CFormSwitch
                  label="Queue Paused"
                  checked={draft.queue_paused}
                  onChange={(e) => set('queue_paused', e.target.checked)}
                />
              </CCol>
              <CCol md={4}>
                <CFormSwitch
                  label="Auto-enqueue on create"
                  checked={draft.auto_enqueue}
                  onChange={(e) => set('auto_enqueue', e.target.checked)}
                />
              </CCol>
              <CCol md={4}>
                <CFormSwitch
                  label="Notifications enabled"
                  checked={draft.notifications_enabled}
                  onChange={(e) => set('notifications_enabled', e.target.checked)}
                />
              </CCol>
            </CRow>

            {/* System LLM — used for Fill Task and other non-agent API calls */}
            <hr className="my-3" />
            <CFormLabel className="fw-semibold">
              System LLM{' '}
              <small className="fw-normal text-body-secondary">
                — used by Fill Task and other non-agent features
              </small>
            </CFormLabel>
            <CRow className="mb-3">
              <CCol md={3}>
                <CFormLabel className="mb-1">Vendor</CFormLabel>
                <CFormSelect
                  value={draft.system_llm_vendor}
                  onChange={(e) => handleVendorChange(e.target.value as AgentVendor)}
                >
                  {AGENT_VENDORS.map((v) => (
                    <option key={v.id} value={v.id}>
                      {v.label}
                    </option>
                  ))}
                </CFormSelect>
              </CCol>
              <CCol md={9}>
                <CFormLabel className="mb-1">Model</CFormLabel>
                <CFormInput
                  value={draft.system_llm_model}
                  onChange={(e) => set('system_llm_model', e.target.value)}
                  list="settings-system-llm-models"
                  placeholder="Type or select a model…"
                  autoComplete="off"
                />
                <datalist id="settings-system-llm-models">
                  {sysModelList.map((m) => (
                    <option key={m.id} value={m.id}>
                      {m.label}
                    </option>
                  ))}
                </datalist>
              </CCol>
            </CRow>

            <CButton
              type="submit"
              color="primary"
              disabled={saving}
            >
              {saving ? 'Saving…' : 'Save Settings'}
            </CButton>
          </CForm>
        </CCardBody>
      </CCard>
    </>
  );
}
