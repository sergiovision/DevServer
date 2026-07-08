'use client';

import { useCallback, useEffect, useState } from 'react';
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
import type { AgentVendor, ClaudeMode } from '@/lib/types';
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
    // Billing mode for system LLM calls (Fill Task, DevPlan, decompose, …):
    // 'max' = subscription via vendor CLI, 'api' = direct API key. Default max.
    system_llm_mode: (unquote(raw.system_llm_mode) || 'max') as ClaudeMode,
    // Migration 010 — memory quality + abstain gate (Pro). 0 / false = off.
    reality_abstain_threshold: Number(raw.reality_abstain_threshold || 0),
    memory_decay_half_life_days: Number(raw.memory_decay_half_life_days || 0),
    memory_archive_days: Number(raw.memory_archive_days || 0),
    memory_iterative_recall: Boolean(raw.memory_iterative_recall),
    // Auto-refresh the code/doc corpus on task success (Pro). Defaults on.
    corpus_auto_index: raw.corpus_auto_index !== false,
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
        ['system_llm_mode', draft.system_llm_mode],
        ['reality_abstain_threshold', draft.reality_abstain_threshold],
        ['memory_decay_half_life_days', draft.memory_decay_half_life_days],
        ['memory_archive_days', draft.memory_archive_days],
        ['memory_iterative_recall', draft.memory_iterative_recall],
        ['corpus_auto_index', draft.corpus_auto_index],
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
              <CCol md={6}>
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
              <CCol md={3}>
                <CFormLabel className="mb-1">Billing</CFormLabel>
                <CFormSelect
                  value={draft.system_llm_mode}
                  onChange={(e) => set('system_llm_mode', e.target.value as ClaudeMode)}
                >
                  <option value="max">Max (subscription)</option>
                  <option value="api">API Platform</option>
                </CFormSelect>
              </CCol>
            </CRow>

            {/* Memory & Reality Gate (Pro). All values 0 / off = disabled. */}
            <hr className="my-3" />
            <CFormLabel className="fw-semibold">
              Memory &amp; Reality Gate{' '}
              <small className="fw-normal text-body-secondary">
                — Pro features. 0 / off keeps prior behaviour.
              </small>
            </CFormLabel>
            <CRow className="mb-3">
              <CCol md={3}>
                <CFormLabel className="mb-1">Abstain threshold</CFormLabel>
                <CFormInput
                  type="number"
                  min="0"
                  max="100"
                  value={String(draft.reality_abstain_threshold)}
                  onChange={(e) => set('reality_abstain_threshold', parseInt(e.target.value) || 0)}
                />
                <small className="text-body-secondary">Block tasks scoring below this (0–100). 0 = off.</small>
              </CCol>
              <CCol md={3}>
                <CFormLabel className="mb-1">Decay half-life (days)</CFormLabel>
                <CFormInput
                  type="number"
                  min="0"
                  value={String(draft.memory_decay_half_life_days)}
                  onChange={(e) => set('memory_decay_half_life_days', parseInt(e.target.value) || 0)}
                />
                <small className="text-body-secondary">Recency weighting in recall. 0 = off (try 90).</small>
              </CCol>
              <CCol md={3}>
                <CFormLabel className="mb-1">Archive after (days)</CFormLabel>
                <CFormInput
                  type="number"
                  min="0"
                  value={String(draft.memory_archive_days)}
                  onChange={(e) => set('memory_archive_days', parseInt(e.target.value) || 0)}
                />
                <small className="text-body-secondary">Archive never-recalled memories. 0 = off (try 180).</small>
              </CCol>
              <CCol md={3} className="d-flex align-items-end">
                <CFormSwitch
                  label="Iterative recall"
                  checked={draft.memory_iterative_recall}
                  onChange={(e) => set('memory_iterative_recall', e.target.checked)}
                />
              </CCol>
              <CCol md={3} className="d-flex flex-column justify-content-end">
                <CFormSwitch
                  label="Auto-index corpus on success"
                  checked={draft.corpus_auto_index}
                  onChange={(e) => set('corpus_auto_index', e.target.checked)}
                />
                <small className="text-body-secondary">Refresh code/doc search index after each task. On by default.</small>
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

      <ConfluenceSettingsCard />
    </>
  );
}

/**
 * Confluence connection (external import source). Values live in .env via
 * the worker's env API — never in the settings table — so the token stays
 * out of the database. Global defaults; repos can carry their own overrides.
 */
function ConfluenceSettingsCard() {
  const [url, setUrl] = useState('');
  const [username, setUsername] = useState('');
  const [token, setToken] = useState('');
  const [loaded, setLoaded] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [pinging, setPinging] = useState(false);
  const [ping, setPing] = useState<{ ok: boolean; error?: string } | null>(null);
  const [error, setError] = useState('');

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch('/api/env', { cache: 'no-store' });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        if (cancelled) return;
        const vars: { key: string; value: string }[] = data.variables || [];
        const val = (k: string) => vars.find((v) => v.key === k)?.value || '';
        setUrl(val('CONFLUENCE_URL'));
        setUsername(val('CONFLUENCE_USERNAME'));
        setToken(val('CONFLUENCE_API_TOKEN'));
      } catch {
        if (!cancelled) setError('Failed to load Confluence configuration');
      } finally {
        if (!cancelled) setLoaded(true);
      }
    })();
    return () => { cancelled = true; };
  }, []);

  const handleSave = useCallback(async () => {
    setSaving(true);
    setSaved(false);
    setError('');
    try {
      const res = await fetch('/api/env', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          variables: {
            CONFLUENCE_URL: url.trim(),
            CONFLUENCE_USERNAME: username.trim(),
            CONFLUENCE_API_TOKEN: token.trim(),
          },
        }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      // Writing .env is not enough — the running worker holds the settings
      // singleton in memory. Reload it so the new credentials take effect
      // immediately (otherwise ping/import keep seeing "not configured"
      // until the next worker restart).
      await fetch('/api/env/apply', { method: 'POST' }).catch(() => {});
      setSaved(true);
      setTimeout(() => setSaved(false), 3000);
    } catch {
      setError('Failed to save Confluence configuration');
    } finally {
      setSaving(false);
    }
  }, [url, username, token]);

  const handlePing = useCallback(async () => {
    setPinging(true);
    setPing(null);
    try {
      const res = await fetch('/api/pro/import/confluence/ping', { cache: 'no-store' });
      setPing(await res.json());
    } catch {
      setPing({ ok: false, error: 'worker unreachable' });
    } finally {
      setPinging(false);
    }
  }, []);

  return (
    <CCard className="mb-4">
      <CCardHeader>
        <strong>Confluence</strong>{' '}
        <small className="text-body-secondary">
          — external import source. Cloud: email + API token. Data Center: PAT, username blank.
        </small>
      </CCardHeader>
      <CCardBody>
        {error && <CAlert color="danger" className="py-2">{error}</CAlert>}
        {saved && <CAlert color="success" className="py-2">Confluence configuration saved.</CAlert>}
        {ping && (
          <CAlert color={ping.ok ? 'success' : 'danger'} className="py-2">
            {ping.ok ? 'Connection OK.' : `Connection failed: ${ping.error || 'unknown error'}`}
          </CAlert>
        )}
        <CForm onSubmit={(e) => { e.preventDefault(); handleSave(); }}>
          <CRow className="mb-3">
            <CCol md={5}>
              <CFormLabel className="mb-1">Base URL</CFormLabel>
              <CFormInput
                type="url"
                placeholder="https://yourorg.atlassian.net"
                value={url}
                disabled={!loaded}
                onChange={(e) => setUrl(e.target.value)}
              />
            </CCol>
            <CCol md={3}>
              <CFormLabel className="mb-1">Username / Email</CFormLabel>
              <CFormInput
                placeholder="you@company.com"
                value={username}
                disabled={!loaded}
                onChange={(e) => setUsername(e.target.value)}
              />
            </CCol>
            <CCol md={4}>
              <CFormLabel className="mb-1">API Token / PAT</CFormLabel>
              <CFormInput
                type="password"
                autoComplete="off"
                value={token}
                disabled={!loaded}
                onChange={(e) => setToken(e.target.value)}
              />
            </CCol>
          </CRow>
          <div className="d-flex gap-2">
            <CButton type="submit" color="primary" disabled={saving || !loaded}>
              {saving ? 'Saving…' : 'Save Confluence Settings'}
            </CButton>
            <CButton
              type="button"
              color="secondary"
              variant="outline"
              disabled={pinging}
              onClick={handlePing}
            >
              {pinging ? 'Pinging…' : 'Ping'}
            </CButton>
          </div>
        </CForm>
      </CCardBody>
    </CCard>
  );
}
