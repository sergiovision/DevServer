'use client';

import React, { useState } from 'react';
import { useRouter } from 'next/navigation';
import {
  CForm,
  CFormLabel,
  CFormInput,
  CFormSelect,
  CFormSwitch,
  CButton,
  CCard,
  CCardBody,
  CCardHeader,
  CRow,
  CCol,
  CAlert,
} from '@coreui/react-pro';
import type { Repo } from '@/lib/types';
import { ModelCombobox } from '@/components/ModelCombobox';

interface RepoFormProps {
  repo?: Repo;
}

export function RepoForm({ repo }: RepoFormProps) {
  const router = useRouter();
  const isEdit = !!repo;

  const [formData, setFormData] = useState({
    name: repo?.name || '',
    gitea_url: repo?.gitea_url || '',
    gitea_owner: repo?.gitea_owner || '',
    gitea_repo: repo?.gitea_repo || '',
    clone_url: repo?.clone_url || '',
    default_branch: repo?.default_branch || 'main',
    build_cmd: repo?.build_cmd || '',
    test_cmd: repo?.test_cmd || '',
    lint_cmd: repo?.lint_cmd || '',
    pre_cmd: repo?.pre_cmd || '',
    claude_model: repo?.claude_model || 'claude-sonnet-4-20250514',
    claude_allowed_tools: repo?.claude_allowed_tools || '',
    gitea_token: repo?.gitea_token || '',
    max_retries: repo?.max_retries?.toString() || '3',
    timeout_minutes: repo?.timeout_minutes?.toString() || '30',
    active: repo?.active ?? true,
  });
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);

  const handleChange = (
    e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>,
  ) => {
    const target = e.target as HTMLInputElement;
    const value = target.type === 'checkbox' ? target.checked : target.value;
    setFormData((prev) => ({ ...prev, [target.name]: value }));
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSaving(true);
    setError('');

    try {
      const body = {
        ...formData,
        max_retries: parseInt(formData.max_retries),
        timeout_minutes: parseInt(formData.timeout_minutes),
      };

      const url = isEdit ? `/api/repos/${repo!.id}` : '/api/repos';
      const method = isEdit ? 'PATCH' : 'POST';

      const res = await fetch(url, {
        method,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });

      if (!res.ok) {
        const data = await res.json();
        throw new Error(data.error || 'Failed to save');
      }

      router.push('/repos');
      router.refresh();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'An error occurred');
    } finally {
      setSaving(false);
    }
  };

  return (
    <CCard>
      <CCardHeader>
        <strong>{isEdit ? 'Edit Repository' : 'Add Repository'}</strong>
      </CCardHeader>
      <CCardBody>
        {error && <CAlert color="danger">{error}</CAlert>}
        <CForm onSubmit={handleSubmit}>
          <CRow className="mb-3">
            <CCol md={6}>
              <CFormLabel>Name</CFormLabel>
              <CFormInput name="name" value={formData.name} onChange={handleChange} required />
            </CCol>
            <CCol md={6}>
              <CFormLabel>Clone URL</CFormLabel>
              <CFormInput name="clone_url" value={formData.clone_url} onChange={handleChange} required />
            </CCol>
          </CRow>

          <CRow className="mb-3">
            <CCol md={3}>
              <CFormLabel>Gitea URL</CFormLabel>
              <CFormInput name="gitea_url" value={formData.gitea_url} onChange={handleChange} />
            </CCol>
            <CCol md={3}>
              <CFormLabel>Gitea Owner</CFormLabel>
              <CFormInput name="gitea_owner" value={formData.gitea_owner} onChange={handleChange} />
            </CCol>
            <CCol md={3}>
              <CFormLabel>Gitea Repo</CFormLabel>
              <CFormInput name="gitea_repo" value={formData.gitea_repo} onChange={handleChange} />
            </CCol>
            <CCol md={3}>
              <CFormLabel>Gitea Token</CFormLabel>
              <CFormInput name="gitea_token" type="password" value={formData.gitea_token} onChange={handleChange} placeholder="API token" />
            </CCol>
          </CRow>

          <CRow className="mb-3">
            <CCol md={3}>
              <CFormLabel>Default Branch</CFormLabel>
              <CFormInput name="default_branch" value={formData.default_branch} onChange={handleChange} />
            </CCol>
            <CCol md={3}>
              <CFormLabel>Default Claude Model</CFormLabel>
              <ModelCombobox
                name="claude_model"
                value={formData.claude_model}
                onChange={(v) => setFormData((prev) => ({ ...prev, claude_model: v }))}
              />
            </CCol>
            <CCol md={3}>
              <CFormLabel>Max Retries</CFormLabel>
              <CFormInput type="number" name="max_retries" value={formData.max_retries} onChange={handleChange} />
            </CCol>
            <CCol md={3}>
              <CFormLabel>Timeout (min)</CFormLabel>
              <CFormInput type="number" name="timeout_minutes" value={formData.timeout_minutes} onChange={handleChange} />
            </CCol>
          </CRow>

          <CRow className="mb-3">
            <CCol md={6}>
              <CFormLabel>Build Command</CFormLabel>
              <CFormInput name="build_cmd" value={formData.build_cmd} onChange={handleChange} />
            </CCol>
            <CCol md={6}>
              <CFormLabel>Test Command</CFormLabel>
              <CFormInput name="test_cmd" value={formData.test_cmd} onChange={handleChange} />
            </CCol>
          </CRow>

          <CRow className="mb-3">
            <CCol md={6}>
              <CFormLabel>Lint Command</CFormLabel>
              <CFormInput name="lint_cmd" value={formData.lint_cmd} onChange={handleChange} />
            </CCol>
            <CCol md={6}>
              <CFormLabel>Pre Command</CFormLabel>
              <CFormInput name="pre_cmd" value={formData.pre_cmd} onChange={handleChange} />
            </CCol>
          </CRow>

          <div className="mb-3">
            <CFormLabel>Allowed Tools</CFormLabel>
            <CFormInput
              name="claude_allowed_tools"
              value={formData.claude_allowed_tools}
              onChange={handleChange}
              placeholder="Comma separated tool names"
            />
          </div>

          <div className="mb-3">
            <CFormSwitch
              name="active"
              label="Active"
              checked={formData.active}
              onChange={handleChange}
            />
          </div>

          <div className="d-flex gap-2">
            <CButton type="submit" color="primary" disabled={saving}>
              {saving ? 'Saving...' : isEdit ? 'Update' : 'Create'}
            </CButton>
            <CButton type="button" color="secondary" variant="outline" onClick={() => router.back()}>
              Cancel
            </CButton>
            {isEdit && (
              <CButton
                type="button"
                color="danger"
                variant="outline"
                className="ms-auto"
                onClick={async () => {
                  if (confirm('Delete this repository?')) {
                    await fetch(`/api/repos/${repo!.id}`, { method: 'DELETE' });
                    router.push('/repos');
                  }
                }}
              >
                Delete
              </CButton>
            )}
          </div>
        </CForm>
      </CCardBody>
    </CCard>
  );
}
