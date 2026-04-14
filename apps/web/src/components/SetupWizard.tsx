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
  CButton,
  CAlert,
  CRow,
  CCol,
  CInputGroup,
  CSpinner,
  CProgress,
  CProgressBar,
} from '@coreui/react-pro';

// ─── Step definitions ──────────────────────────────────────────────────────

interface FieldDef {
  key: string;
  label: string;
  type: 'text' | 'password' | 'number' | 'path' | 'url' | 'boolean';
  placeholder?: string;
  hint?: string;
  required?: boolean;
}

interface StepDef {
  id: string;
  title: string;
  description: string;
  fields: FieldDef[];
  optional?: boolean;
}

const STEPS: StepDef[] = [
  {
    id: 'welcome',
    title: 'Welcome to DevServer',
    description:
      'This wizard will guide you through the initial configuration. ' +
      'All values are saved to the .env file and can be changed later from the Settings page.',
    fields: [],
  },
  {
    id: 'paths',
    title: 'Paths',
    description: 'Where DevServer stores worktrees and logs.',
    fields: [
      {
        key: 'DEVSERVER_ROOT',
        label: 'DevServer Root',
        type: 'path',
        hint: 'Absolute path to this repository on disk.',
        required: true,
      },
      {
        key: 'WORKTREE_DIR',
        label: 'Worktree Directory',
        type: 'path',
        hint: 'Where git worktrees are created. Use ${DEVSERVER_ROOT}/worktrees for the default.',
      },
      {
        key: 'LOG_DIR',
        label: 'Log Directory',
        type: 'path',
        hint: 'Where task logs are written. Use ${DEVSERVER_ROOT}/logs/tasks for the default.',
      },
    ],
  },
  {
    id: 'git',
    title: 'Git & Gitea',
    description: 'Git identity for agent commits and Gitea connection for PR creation.',
    fields: [
      {
        key: 'GIT_USER_EMAIL',
        label: 'Git Email',
        type: 'text',
        placeholder: 'devserver@example.com',
        required: true,
      },
      {
        key: 'GIT_USER_NAME',
        label: 'Git Name',
        type: 'text',
        placeholder: 'DevServer Agent',
        required: true,
      },
      {
        key: 'GIT_SSL_NO_VERIFY',
        label: 'Skip SSL verification',
        type: 'boolean',
        hint: 'Enable only for self-signed certs.',
      },
      {
        key: 'GITEA_URL',
        label: 'Gitea URL',
        type: 'url',
        placeholder: 'https://your-gitea.example.com',
        required: true,
      },
      {
        key: 'GITEA_OWNER',
        label: 'Gitea Owner / Org',
        type: 'text',
        placeholder: 'your-org',
        required: true,
      },
      {
        key: 'GITEA_TOKEN',
        label: 'Gitea Personal Access Token',
        type: 'password',
        required: true,
      },
    ],
  },
  {
    id: 'telegram',
    title: 'Telegram Notifications',
    description:
      'Optional but recommended. The worker posts real-time task updates here. ' +
      'Create a bot via @BotFather and get your chat ID from @userinfobot.',
    optional: true,
    fields: [
      {
        key: 'TELEGRAM_BOT_TOKEN',
        label: 'Bot Token',
        type: 'password',
        placeholder: '123456:ABC-...',
      },
      {
        key: 'TELEGRAM_CHAT_ID',
        label: 'Chat ID',
        type: 'text',
        placeholder: '123456789',
      },
    ],
  },
  {
    id: 'ai',
    title: 'AI Backends',
    description:
      'At least one API key is required. Anthropic (Claude) is the primary production-tested backend.',
    fields: [
      {
        key: 'ANTHROPIC_API_KEY',
        label: 'Anthropic API Key',
        type: 'password',
        placeholder: 'sk-ant-api03-...',
        hint: 'Primary backend. Required unless using Max subscription.',
      },
      {
        key: 'CLAUDE_BIN',
        label: 'Claude CLI Binary',
        type: 'text',
        placeholder: 'claude',
      },
      {
        key: 'CLAUDE_MAX_TIMEOUT',
        label: 'Max Timeout (seconds)',
        type: 'number',
        placeholder: '3600',
      },
      {
        key: 'GLM_API_KEY',
        label: 'GLM / Zhipu API Key',
        type: 'password',
        hint: 'Optional. Used as system LLM and fallback agent backend.',
      },
      {
        key: 'OPENAI_API_KEY',
        label: 'OpenAI API Key',
        type: 'password',
        hint: 'Optional fallback backend.',
      },
      {
        key: 'GEMINI_API_KEY',
        label: 'Google Gemini API Key',
        type: 'password',
        hint: 'Optional fallback backend.',
      },
    ],
  },
  {
    id: 'review',
    title: 'Review & Complete',
    description: 'Review your configuration and finish the setup.',
    fields: [],
  },
];

const TOTAL_STEPS = STEPS.length;

// ─── Component ─────────────────────────────────────────────────────────────

export function SetupWizard() {
  const [step, setStep] = useState(0);
  const [values, setValues] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [showSecrets, setShowSecrets] = useState<Record<string, boolean>>({});

  // Load current env values on mount
  useEffect(() => {
    fetch('/api/env')
      .then((r) => r.json())
      .then((data) => {
        if (data.variables) {
          const vals: Record<string, string> = {};
          for (const v of data.variables) {
            vals[v.key] = v.value;
          }
          setValues(vals);
        }
      })
      .catch(() => {
        // Worker might not be running yet — allow manual entry
      })
      .finally(() => setLoading(false));
  }, []);

  const handleChange = useCallback((key: string, value: string) => {
    setValues((prev) => ({ ...prev, [key]: value }));
  }, []);

  const toggleSecret = useCallback((key: string) => {
    setShowSecrets((prev) => ({ ...prev, [key]: !prev[key] }));
  }, []);

  const handleFinish = useCallback(async () => {
    setSaving(true);
    setError('');
    try {
      // 1. Save all values to .env
      const res = await fetch('/api/env', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ variables: values }),
      });
      if (!res.ok) throw new Error('Failed to save configuration');

      // 2. Apply config to running worker
      await fetch('/api/env/apply', { method: 'POST' });

      // 3. Mark setup as completed (DB + cookie)
      const complete = await fetch('/api/setup/complete', { method: 'POST' });
      if (!complete.ok) throw new Error('Failed to mark setup as completed');

      // 4. Redirect to dashboard
      window.location.href = '/';
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Setup failed');
    } finally {
      setSaving(false);
    }
  }, [values]);

  const current = STEPS[step];
  const progress = Math.round(((step + 1) / TOTAL_STEPS) * 100);

  // ─── Render helpers ────────────────────────────────────────────────────

  const renderField = (f: FieldDef) => {
    if (f.type === 'boolean') {
      return (
        <CCol md={6} key={f.key}>
          <CFormSwitch
            label={f.label}
            checked={values[f.key] === 'true'}
            onChange={(e) =>
              handleChange(f.key, e.target.checked ? 'true' : 'false')
            }
          />
          {f.hint && (
            <small className="text-body-secondary d-block">{f.hint}</small>
          )}
        </CCol>
      );
    }

    if (f.type === 'password') {
      const visible = showSecrets[f.key];
      return (
        <CCol md={6} key={f.key}>
          <CFormLabel className="mb-1">
            {f.label}
            {f.required && <span className="text-danger ms-1">*</span>}
          </CFormLabel>
          <CInputGroup size="sm">
            <CFormInput
              type={visible ? 'text' : 'password'}
              value={values[f.key] || ''}
              onChange={(e) => handleChange(f.key, e.target.value)}
              placeholder={f.placeholder}
              autoComplete="off"
            />
            <CButton
              color="secondary"
              variant="outline"
              size="sm"
              onClick={() => toggleSecret(f.key)}
            >
              {visible ? 'Hide' : 'Show'}
            </CButton>
          </CInputGroup>
          {f.hint && (
            <small className="text-body-secondary d-block mt-1">
              {f.hint}
            </small>
          )}
        </CCol>
      );
    }

    return (
      <CCol md={6} key={f.key}>
        <CFormLabel className="mb-1">
          {f.label}
          {f.required && <span className="text-danger ms-1">*</span>}
        </CFormLabel>
        <CFormInput
          size="sm"
          type={f.type === 'number' ? 'number' : 'text'}
          value={values[f.key] || ''}
          onChange={(e) => handleChange(f.key, e.target.value)}
          placeholder={f.placeholder}
          autoComplete="off"
        />
        {f.hint && (
          <small className="text-body-secondary d-block mt-1">{f.hint}</small>
        )}
      </CCol>
    );
  };

  const renderReview = () => {
    // Group configured values by step
    const configured = STEPS.filter((s) => s.fields.length > 0).map((s) => ({
      title: s.title,
      fields: s.fields
        .filter((f) => values[f.key])
        .map((f) => ({
          label: f.label,
          value: f.type === 'password' ? '\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022' : values[f.key],
          key: f.key,
        })),
    }));

    return (
      <div>
        {configured.map((group) => (
          <div key={group.title} className="mb-3">
            <strong className="small text-uppercase text-body-secondary">
              {group.title}
            </strong>
            {group.fields.length === 0 ? (
              <div className="text-body-secondary small">
                No values configured
              </div>
            ) : (
              <table className="table table-sm table-borderless mb-0 mt-1">
                <tbody>
                  {group.fields.map((f) => (
                    <tr key={f.key}>
                      <td className="text-body-secondary" style={{ width: '40%' }}>
                        {f.label}
                      </td>
                      <td className="font-monospace small">{f.value}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        ))}
      </div>
    );
  };

  // ─── Main render ───────────────────────────────────────────────────────

  if (loading) {
    return (
      <div className="d-flex justify-content-center align-items-center min-vh-100">
        <CSpinner />
      </div>
    );
  }

  return (
    <div
      className="d-flex justify-content-center align-items-start min-vh-100 py-5"
      style={{ backgroundColor: 'var(--cui-body-bg, #f8f9fa)' }}
    >
      <div style={{ width: '100%', maxWidth: 720 }}>
        {/* Header */}
        <div className="text-center mb-4">
          <h2 className="fw-bold mb-1">DevServer</h2>
          <small className="text-body-secondary">
            Step {step + 1} of {TOTAL_STEPS}
          </small>
        </div>

        {/* Progress bar */}
        <CProgress className="mb-4" style={{ height: 6 }}>
          <CProgressBar value={progress} />
        </CProgress>

        {/* Step card */}
        <CCard>
          <CCardHeader>
            <strong>{current.title}</strong>
            {current.optional && (
              <span className="badge bg-secondary ms-2 fw-normal">
                Optional
              </span>
            )}
          </CCardHeader>
          <CCardBody>
            <p className="text-body-secondary mb-3">{current.description}</p>

            {error && (
              <CAlert color="danger" className="py-2">
                {error}
              </CAlert>
            )}

            {current.id === 'review' ? (
              renderReview()
            ) : current.fields.length > 0 ? (
              <CForm>
                <CRow className="g-3">{current.fields.map(renderField)}</CRow>
              </CForm>
            ) : null}

            {/* Navigation */}
            <hr />
            <div className="d-flex justify-content-between">
              <CButton
                color="secondary"
                variant="outline"
                disabled={step === 0}
                onClick={() => {
                  setStep((s) => s - 1);
                  setError('');
                }}
              >
                Back
              </CButton>

              <div className="d-flex gap-2">
                {current.optional && (
                  <CButton
                    color="secondary"
                    variant="ghost"
                    onClick={() => setStep((s) => s + 1)}
                  >
                    Skip
                  </CButton>
                )}

                {step < TOTAL_STEPS - 1 ? (
                  <CButton
                    color="primary"
                    onClick={() => {
                      setStep((s) => s + 1);
                      setError('');
                    }}
                  >
                    {step === 0 ? 'Get Started' : 'Next'}
                  </CButton>
                ) : (
                  <CButton
                    color="success"
                    disabled={saving}
                    onClick={handleFinish}
                  >
                    {saving ? (
                      <>
                        <CSpinner size="sm" className="me-1" /> Finishing...
                      </>
                    ) : (
                      'Save & Launch'
                    )}
                  </CButton>
                )}
              </div>
            </div>
          </CCardBody>
        </CCard>

        {/* Skip setup link */}
        <div className="text-center mt-3">
          <button
            className="btn btn-link btn-sm text-body-secondary text-decoration-none"
            onClick={async () => {
              await fetch('/api/setup/complete', { method: 'POST' });
              window.location.href = '/';
            }}
          >
            Skip setup — I already configured .env manually
          </button>
        </div>
      </div>
    </div>
  );
}
