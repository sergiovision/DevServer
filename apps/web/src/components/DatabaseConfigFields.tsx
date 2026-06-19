'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  CCard,
  CCardBody,
  CCardHeader,
  CForm,
  CFormCheck,
  CFormLabel,
  CFormInput,
  CInputGroup,
  CButton,
  CAlert,
  CRow,
  CCol,
  CSpinner,
} from '@coreui/react-pro';

// ─── Mode registry ───────────────────────────────────────────────────────────
//
// The DB connection lives in .env (PG* + DATABASE_URL), NOT the settings table
// — that table is *inside* the database, so it can't hold the connection that
// reaches it. A "mode" is a UX convenience that prefills/locks the right env
// values; the user always ends up editing the same handful of PG* vars.

export interface DbMode {
  id: string;
  label: string;
  hint: string;
  /** Field values applied (overwriting) when this mode is selected. */
  defaults: Partial<Record<PgKey, string>>;
  /** When true, host/port/user/database are read-only (only the password is editable). */
  locked: boolean;
  /** DEVSERVER_HOST_DB value to write (docker topology hint for the lifecycle scripts). */
  hostDb?: '0' | '1';
}

type PgKey = 'PGHOST' | 'PGPORT' | 'PGUSER' | 'PGPASSWORD' | 'PGDATABASE';

const LOCAL_DEFAULTS: Partial<Record<PgKey, string>> = {
  PGHOST: '127.0.0.1',
  PGPORT: '5432',
  PGUSER: 'devserver',
  PGDATABASE: 'devserver',
};

const HOST_MODES: DbMode[] = [
  {
    id: 'local',
    label: 'Local PostgreSQL (default)',
    hint: 'A PostgreSQL running on this machine at 127.0.0.1:5432 with the default devserver user/database. You only set the password.',
    defaults: LOCAL_DEFAULTS,
    locked: true,
  },
  {
    id: 'custom',
    label: 'Custom host / credentials',
    hint: 'Point at any PostgreSQL — set your own host, port, user, password and database.',
    defaults: {},
    locked: false,
  },
];

const DOCKER_MODES: DbMode[] = [
  {
    id: 'docker',
    label: 'Docker PostgreSQL (bundled)',
    hint: 'The bundled pgvector container managed by docker compose. The host worker reaches it at 127.0.0.1:5432; the web container reaches it via the "postgres" service name.',
    defaults: LOCAL_DEFAULTS,
    locked: true,
    hostDb: '0',
  },
  {
    id: 'host',
    label: 'Host OS PostgreSQL',
    hint: 'A PostgreSQL installed on the host OS instead of the bundled container. The bundled DB container is skipped (DEVSERVER_HOST_DB=1).',
    defaults: LOCAL_DEFAULTS,
    locked: false,
    hostDb: '1',
  },
  {
    id: 'external',
    label: 'External host / credentials',
    hint: 'A remote PostgreSQL server — set explicit host, port, user, password and database. The bundled DB container is skipped.',
    defaults: {},
    locked: false,
    hostDb: '1',
  },
];

export function modesForDeploy(deployMode: string): DbMode[] {
  return deployMode === 'docker' ? DOCKER_MODES : HOST_MODES;
}

/** Best-effort: pick the mode that matches the current env values on first load. */
export function deriveMode(deployMode: string, v: Record<string, string>): string {
  const host = (v.PGHOST || '').trim().toLowerCase();
  const user = (v.PGUSER || '').trim() || 'devserver';
  const db = (v.PGDATABASE || '').trim() || 'devserver';
  const localHost = host === '' || host === '127.0.0.1' || host === 'localhost' || host === 'postgres';
  const looksDefault = localHost && user === 'devserver' && db === 'devserver';
  const hostDbOn = ['1', 'true', 'yes'].includes((v.DEVSERVER_HOST_DB || '').trim().toLowerCase());
  if (deployMode === 'docker') {
    if (hostDbOn) return looksDefault ? 'host' : 'external';
    return looksDefault ? 'docker' : 'external';
  }
  return looksDefault ? 'local' : 'custom';
}

/**
 * Container-perspective DB host/port for a docker deployment. A container can't
 * reach the host's 127.0.0.1, and the bundled DB answers on the internal port
 * 5432 (not the host-published PGPORT) — so these differ from the host-side PG*:
 *   • docker (bundled) → the `postgres` service on its internal port 5432
 *   • host             → host.docker.internal on the host DB's port
 *   • external         → the remote host:port as typed
 * Returns null for non-docker modes (the host runs web directly, no remap).
 */
export function containerDbTarget(
  mode: string,
  pghost: string,
  pgport: string,
): { host: string; port: string } | null {
  const port = (pgport || '').trim() || '5432';
  switch (mode) {
    case 'docker':
      return { host: 'postgres', port: '5432' };
    case 'host':
      return { host: 'host.docker.internal', port };
    case 'external':
      return { host: (pghost || '').trim() || 'host.docker.internal', port };
    default:
      return null;
  }
}

/** Compose a libpq DATABASE_URL from the discrete fields (password URL-encoded). */
export function buildDatabaseUrl(v: Record<string, string>): string {
  const user = encodeURIComponent(v.PGUSER || 'devserver');
  const pass = encodeURIComponent(v.PGPASSWORD || '');
  const host = v.PGHOST || '127.0.0.1';
  const port = v.PGPORT || '5432';
  const db = v.PGDATABASE || 'devserver';
  const auth = pass ? `${user}:${pass}` : user;
  return `postgresql://${auth}@${host}:${port}/${db}`;
}

// ─── Reusable controlled selector ─────────────────────────────────────────────

interface FieldsProps {
  deployMode: string;
  values: Record<string, string>;
  /** Apply one or more env keys to the parent state. */
  onChange: (key: string, value: string) => void;
}

const PG_TEXT_FIELDS: { key: PgKey; label: string; type: string; col: number }[] = [
  { key: 'PGHOST', label: 'Host', type: 'text', col: 8 },
  { key: 'PGPORT', label: 'Port', type: 'number', col: 4 },
  { key: 'PGUSER', label: 'User', type: 'text', col: 6 },
  { key: 'PGDATABASE', label: 'Database', type: 'text', col: 6 },
];

export function DatabaseConfigFields({ deployMode, values, onChange }: FieldsProps) {
  const modes = useMemo(() => modesForDeploy(deployMode), [deployMode]);
  const [mode, setMode] = useState<string>(() => deriveMode(deployMode, values));
  const [showPw, setShowPw] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<{ ok: boolean; msg: string } | null>(null);

  const active = modes.find((m) => m.id === mode) || modes[0];

  // Commit a set of field updates AND keep DATABASE_URL (host perspective) plus
  // the container-perspective vars in sync. `effectiveMode` lets selectMode pass
  // the just-picked mode without waiting for the async state update.
  const commit = useCallback(
    (updates: Record<string, string>, effectiveMode: string = mode) => {
      const merged = { ...values, ...updates };
      const out: Record<string, string> = { ...updates, DATABASE_URL: buildDatabaseUrl(merged) };
      if (deployMode === 'docker') {
        const t = containerDbTarget(effectiveMode, merged.PGHOST || '', merged.PGPORT || '');
        if (t) {
          out.PGHOST_CONTAINER = t.host;
          out.PGPORT_CONTAINER = t.port;
        }
      }
      Object.entries(out).forEach(([k, val]) => onChange(k, val));
      setTestResult(null);
    },
    [values, onChange, deployMode, mode],
  );

  const selectMode = useCallback(
    (m: DbMode) => {
      setMode(m.id);
      const updates: Record<string, string> = { ...m.defaults } as Record<string, string>;
      if (m.hostDb !== undefined) updates.DEVSERVER_HOST_DB = m.hostDb;
      commit(updates, m.id);
    },
    [commit],
  );

  const setField = useCallback((key: PgKey, val: string) => commit({ [key]: val }), [commit]);

  // Fresh install: if the connection fields are empty, seed the default
  // (locked) mode's values so clicking "Next" persists something sensible.
  const didInit = useRef(false);
  useEffect(() => {
    if (didInit.current) return;
    didInit.current = true;
    const empty = !values.PGHOST && !values.PGUSER && !values.PGDATABASE;
    if (empty && active.locked) {
      const updates: Record<string, string> = { ...active.defaults } as Record<string, string>;
      if (active.hostDb !== undefined) updates.DEVSERVER_HOST_DB = active.hostDb;
      commit(updates);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const runTest = useCallback(async () => {
    setTesting(true);
    setTestResult(null);
    try {
      const res = await fetch('/api/env/test-db', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          host: values.PGHOST || '127.0.0.1',
          port: parseInt(values.PGPORT || '5432', 10),
          user: values.PGUSER || 'devserver',
          password: values.PGPASSWORD || '',
          database: values.PGDATABASE || 'devserver',
        }),
      });
      const data = await res.json();
      if (data.ok) {
        const v = String(data.version || '').split(' ').slice(0, 2).join(' ');
        setTestResult({ ok: true, msg: `Connected${v ? ` — ${v}` : ''}` });
      } else {
        setTestResult({ ok: false, msg: data.error || 'Connection failed' });
      }
    } catch {
      setTestResult({ ok: false, msg: 'Could not reach the worker to run the test.' });
    } finally {
      setTesting(false);
    }
  }, [values]);

  return (
    <div>
      {/* Mode selection */}
      <div className="mb-3">
        {modes.map((m) => (
          <div key={m.id} className="mb-2">
            <CFormCheck
              type="radio"
              name="db-mode"
              id={`db-mode-${m.id}`}
              checked={mode === m.id}
              onChange={() => selectMode(m)}
              label={<span className="fw-semibold">{m.label}</span>}
            />
            <small className="text-body-secondary d-block ms-4">{m.hint}</small>
          </div>
        ))}
      </div>

      {/* Connection fields */}
      <CForm>
        <CRow className="g-3">
          {PG_TEXT_FIELDS.map((f) => (
            <CCol md={f.col} key={f.key}>
              <CFormLabel className="mb-1">{f.label}</CFormLabel>
              <CFormInput
                size="sm"
                type={f.type === 'number' ? 'number' : 'text'}
                value={values[f.key] || ''}
                disabled={active.locked}
                onChange={(e) => setField(f.key, e.target.value)}
                autoComplete="off"
              />
            </CCol>
          ))}
          <CCol md={12}>
            <CFormLabel className="mb-1">Password</CFormLabel>
            <CInputGroup size="sm">
              <CFormInput
                type={showPw ? 'text' : 'password'}
                value={values.PGPASSWORD || ''}
                onChange={(e) => setField('PGPASSWORD', e.target.value)}
                autoComplete="off"
              />
              <CButton color="secondary" variant="outline" size="sm" onClick={() => setShowPw((s) => !s)}>
                {showPw ? 'Hide' : 'Show'}
              </CButton>
            </CInputGroup>
          </CCol>
        </CRow>
      </CForm>

      {active.locked && (
        <small className="text-body-secondary d-block mt-2">
          Host, port, user and database are fixed for this mode — pick a “custom / external” mode to edit them.
        </small>
      )}

      {/* Test connection */}
      <div className="d-flex align-items-center gap-2 mt-3">
        <CButton color="secondary" variant="outline" size="sm" disabled={testing} onClick={runTest}>
          {testing ? (
            <>
              <CSpinner size="sm" className="me-1" /> Testing…
            </>
          ) : (
            'Test connection'
          )}
        </CButton>
        {testResult && (
          <span className={`small ${testResult.ok ? 'text-success' : 'text-danger'}`}>
            {testResult.ok ? '✓ ' : '✗ '}
            {testResult.msg}
          </span>
        )}
      </div>

      {deployMode === 'docker' && (mode === 'host' || mode === 'external') && (
        <CAlert color="info" className="py-2 mt-3 mb-0 small">
          The bundled <code>postgres</code> container will be skipped
          (<code>DEVSERVER_HOST_DB=1</code>). The web container reaches a non-bundled
          DB via <code>host.docker.internal</code> / the host you set above.
        </CAlert>
      )}
    </div>
  );
}

// ─── Standalone card for the Settings page ────────────────────────────────────

const PG_KEYS = [
  'PGHOST',
  'PGPORT',
  'PGUSER',
  'PGPASSWORD',
  'PGDATABASE',
  'DATABASE_URL',
  'DEVSERVER_HOST_DB',
  'PGHOST_CONTAINER',
  'PGPORT_CONTAINER',
];

export function DatabaseSettingsCard({ deployMode = 'development' }: { deployMode?: string }) {
  const [values, setValues] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState<{ color: string; text: string } | null>(null);

  useEffect(() => {
    fetch('/api/env')
      .then((r) => r.json())
      .then((data) => {
        const vals: Record<string, string> = {};
        if (data.variables) {
          for (const v of data.variables) {
            if (PG_KEYS.includes(v.key)) vals[v.key] = v.value;
          }
        }
        setValues(vals);
      })
      .catch(() => setMsg({ color: 'warning', text: 'Could not load current DB config from the worker.' }))
      .finally(() => setLoading(false));
  }, []);

  const onChange = useCallback((key: string, value: string) => {
    setValues((prev) => ({ ...prev, [key]: value }));
    setMsg(null);
  }, []);

  const save = useCallback(async () => {
    setSaving(true);
    setMsg(null);
    try {
      const payload: Record<string, string> = {};
      for (const k of PG_KEYS) if (values[k] !== undefined) payload[k] = values[k];
      const res = await fetch('/api/env', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ variables: payload }),
      });
      if (!res.ok) throw new Error('Failed to save');
      await fetch('/api/env/apply', { method: 'POST' });
      setMsg({
        color: 'success',
        text: 'Saved. Database changes require a worker (and web) restart to take effect.',
      });
    } catch (e) {
      setMsg({ color: 'danger', text: e instanceof Error ? e.message : 'Save failed' });
    } finally {
      setSaving(false);
    }
  }, [values]);

  return (
    <CCard className="mb-4">
      <CCardHeader>
        <strong>Database</strong>
        <span className="badge bg-secondary ms-2 fw-normal text-uppercase">{deployMode}</span>
      </CCardHeader>
      <CCardBody>
        {loading ? (
          <CSpinner size="sm" />
        ) : (
          <>
            {msg && (
              <CAlert color={msg.color} className="py-2">
                {msg.text}
              </CAlert>
            )}
            <DatabaseConfigFields deployMode={deployMode} values={values} onChange={onChange} />
            <hr />
            <CButton color="primary" disabled={saving} onClick={save}>
              {saving ? (
                <>
                  <CSpinner size="sm" className="me-1" /> Saving…
                </>
              ) : (
                'Save database settings'
              )}
            </CButton>
          </>
        )}
      </CCardBody>
    </CCard>
  );
}
