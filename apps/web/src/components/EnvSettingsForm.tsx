'use client';

import React, { useCallback, useEffect, useState } from 'react';
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
  CCollapse,
  CInputGroup,
  CSpinner,
  CBadge,
} from '@coreui/react-pro';

interface EnvVar {
  key: string;
  group: string;
  label: string;
  type: string;
  secret: boolean;
  value: string;
  options?: string[];
  restart?: boolean;
}

interface EnvGroup {
  name: string;
  vars: EnvVar[];
}

export function EnvSettingsForm() {
  const [variables, setVariables] = useState<EnvVar[]>([]);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [applying, setApplying] = useState(false);
  const [saved, setSaved] = useState(false);
  const [applied, setApplied] = useState(false);
  const [error, setError] = useState('');
  const [showSecrets, setShowSecrets] = useState<Record<string, boolean>>({});
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  const [dirty, setDirty] = useState(false);
  const [envPath, setEnvPath] = useState('');

  // Load env vars on mount
  useEffect(() => {
    fetch('/api/env')
      .then((r) => r.json())
      .then((data) => {
        if (data.error) {
          setError(data.error);
          return;
        }
        setVariables(data.variables || []);
        setEnvPath(data.env_path || '');
        const d: Record<string, string> = {};
        for (const v of data.variables || []) {
          d[v.key] = v.value;
        }
        setDraft(d);
      })
      .catch(() => setError('Failed to load environment configuration'))
      .finally(() => setLoading(false));
  }, []);

  // Group variables by their group field
  const groups: EnvGroup[] = [];
  const groupMap = new Map<string, EnvVar[]>();
  for (const v of variables) {
    if (!groupMap.has(v.group)) {
      groupMap.set(v.group, []);
      groups.push({ name: v.group, vars: groupMap.get(v.group)! });
    }
    groupMap.get(v.group)!.push(v);
  }

  const handleChange = useCallback((key: string, value: string) => {
    setDraft((prev) => ({ ...prev, [key]: value }));
    setDirty(true);
    setSaved(false);
    setApplied(false);
  }, []);

  const handleSave = useCallback(async () => {
    setSaving(true);
    setError('');
    try {
      const changed: Record<string, string> = {};
      for (const v of variables) {
        if (draft[v.key] !== v.value) {
          changed[v.key] = draft[v.key];
        }
      }
      if (Object.keys(changed).length === 0) {
        setSaved(true);
        setTimeout(() => setSaved(false), 3000);
        setSaving(false);
        return;
      }
      const res = await fetch('/api/env', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ variables: changed }),
      });
      if (!res.ok) throw new Error('Save failed');
      setSaved(true);
      setDirty(false);
      // Update original values to track future dirtiness
      setVariables((prev) =>
        prev.map((v) =>
          changed[v.key] !== undefined ? { ...v, value: changed[v.key] } : v,
        ),
      );
      setTimeout(() => setSaved(false), 3000);
    } catch {
      setError('Failed to save environment variables');
    } finally {
      setSaving(false);
    }
  }, [draft, variables]);

  const handleApply = useCallback(async () => {
    setApplying(true);
    setError('');
    try {
      const res = await fetch('/api/env/apply', { method: 'POST' });
      if (!res.ok) throw new Error('Apply failed');
      setApplied(true);
      setTimeout(() => setApplied(false), 5000);
    } catch {
      setError('Failed to apply configuration. The worker may need a manual restart.');
    } finally {
      setApplying(false);
    }
  }, []);

  const toggleSecret = useCallback((key: string) => {
    setShowSecrets((prev) => ({ ...prev, [key]: !prev[key] }));
  }, []);

  const toggleGroup = useCallback((group: string) => {
    setCollapsed((prev) => ({ ...prev, [group]: !prev[group] }));
  }, []);

  if (loading) {
    return (
      <CCard className="mb-4">
        <CCardBody className="text-center py-4">
          <CSpinner size="sm" className="me-2" />
          Loading environment configuration...
        </CCardBody>
      </CCard>
    );
  }

  const renderField = (v: EnvVar) => {
    const isChanged = draft[v.key] !== v.value;

    if (v.type === 'boolean') {
      return (
        <CFormSwitch
          label={v.label}
          checked={draft[v.key] === 'true'}
          onChange={(e) => handleChange(v.key, e.target.checked ? 'true' : 'false')}
        />
      );
    }

    if (v.type === 'select' && v.options) {
      return (
        <>
          <CFormLabel className="mb-1 small">{v.label}</CFormLabel>
          <CFormSelect
            size="sm"
            value={draft[v.key] || ''}
            onChange={(e) => handleChange(v.key, e.target.value)}
            className={isChanged ? 'border-warning' : ''}
          >
            {v.options.map((opt) => (
              <option key={opt} value={opt}>
                {opt}
              </option>
            ))}
          </CFormSelect>
        </>
      );
    }

    if (v.secret) {
      const visible = showSecrets[v.key];
      return (
        <>
          <CFormLabel className="mb-1 small">{v.label}</CFormLabel>
          <CInputGroup size="sm">
            <CFormInput
              type={visible ? 'text' : 'password'}
              value={draft[v.key] || ''}
              onChange={(e) => handleChange(v.key, e.target.value)}
              className={isChanged ? 'border-warning' : ''}
              autoComplete="off"
            />
            <CButton
              color="secondary"
              variant="outline"
              size="sm"
              onClick={() => toggleSecret(v.key)}
              title={visible ? 'Hide' : 'Show'}
            >
              {visible ? 'Hide' : 'Show'}
            </CButton>
          </CInputGroup>
        </>
      );
    }

    return (
      <>
        <CFormLabel className="mb-1 small">{v.label}</CFormLabel>
        <CFormInput
          size="sm"
          type={v.type === 'number' ? 'number' : 'text'}
          value={draft[v.key] || ''}
          onChange={(e) => handleChange(v.key, e.target.value)}
          className={isChanged ? 'border-warning' : ''}
          autoComplete="off"
        />
      </>
    );
  };

  // Check if any changed var requires restart
  const changedRequiresRestart = variables.some(
    (v) => v.restart && draft[v.key] !== v.value,
  );

  return (
    <CCard className="mb-4">
      <CCardHeader className="d-flex justify-content-between align-items-center">
        <strong>Environment Variables</strong>
        <div className="d-flex align-items-center gap-3">
          {envPath && (
            <small className="text-body-secondary font-monospace">{envPath}</small>
          )}
          <CButton
            size="sm"
            color="secondary"
            variant="outline"
            onClick={() => {
              window.location.href = '/setup?force=1';
            }}
            title="Re-run the first-boot setup wizard to configure env vars step-by-step"
          >
            Run Setup
          </CButton>
        </div>
      </CCardHeader>
      <CCardBody>
        {error && (
          <CAlert color="danger" className="py-2" dismissible onClose={() => setError('')}>
            {error}
          </CAlert>
        )}
        {saved && (
          <CAlert color="success" className="py-2">
            Changes saved to .env file.
          </CAlert>
        )}
        {applied && (
          <CAlert color="info" className="py-2">
            Configuration applied to running worker.
            {changedRequiresRestart && (
              <>
                {' '}
                <strong>Note:</strong> Some changed settings (marked with{' '}
                <CBadge color="warning" size="sm">restart</CBadge>) require a
                full worker restart to take effect.
              </>
            )}
          </CAlert>
        )}

        <CForm
          onSubmit={(e) => {
            e.preventDefault();
            handleSave();
          }}
        >
          {groups.map((group) => {
            const isOpen = !collapsed[group.name];
            const hasChanges = group.vars.some(
              (v) => draft[v.key] !== v.value,
            );
            return (
              <div key={group.name} className="mb-3">
                <div
                  className="d-flex align-items-center border-bottom pb-1 mb-2"
                  onClick={() => toggleGroup(group.name)}
                  role="button"
                  style={{ cursor: 'pointer' }}
                >
                  <span className="me-2">{isOpen ? '\u25BC' : '\u25B6'}</span>
                  <strong className="small text-uppercase text-body-secondary">
                    {group.name}
                  </strong>
                  {hasChanges && (
                    <CBadge color="warning" size="sm" className="ms-2">
                      modified
                    </CBadge>
                  )}
                  {group.vars.some((v) => v.restart) && (
                    <CBadge color="secondary" size="sm" className="ms-2">
                      restart
                    </CBadge>
                  )}
                </div>
                <CCollapse visible={isOpen}>
                  <CRow className="g-2 mb-2">
                    {group.vars.map((v) => (
                      <CCol key={v.key} md={v.type === 'boolean' ? 3 : 6}>
                        {renderField(v)}
                      </CCol>
                    ))}
                  </CRow>
                </CCollapse>
              </div>
            );
          })}

          <hr />
          <div className="d-flex gap-2 align-items-center">
            <CButton type="submit" color="primary" disabled={saving || !dirty}>
              {saving ? (
                <>
                  <CSpinner size="sm" className="me-1" /> Saving...
                </>
              ) : (
                'Save to .env'
              )}
            </CButton>
            <CButton
              color="success"
              disabled={applying || dirty}
              onClick={handleApply}
              title={
                dirty
                  ? 'Save changes first before applying'
                  : 'Reload config in running worker'
              }
            >
              {applying ? (
                <>
                  <CSpinner size="sm" className="me-1" /> Applying...
                </>
              ) : (
                'Apply'
              )}
            </CButton>
          </div>
          {dirty && (
            <small className="text-warning d-block mt-1">
              Unsaved changes — save first, then click Apply to reload the
              running worker config.
            </small>
          )}
        </CForm>
      </CCardBody>
    </CCard>
  );
}
