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
    provider: repo?.provider || 'gitea',
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

  const isLocal = formData.provider === 'local';

  const handleChange = (
    e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>,
  ) => {
    const target = e.target as HTMLInputElement;
    const value = target.type === 'checkbox' ? target.checked : target.value;
    setFormData((prev) => {
      const next = { ...prev, [target.name]: value };
      // Auto-detect the provider from the clone URL host as the user types
      // it. They can still override via the Provider select afterwards.
      // An explicitly chosen Local Git provider is never overridden.
      if (
        target.name === 'clone_url' &&
        typeof value === 'string' &&
        prev.provider !== 'local'
      ) {
        next.provider = /^https?:\/\/([^/@]+@)?github\.com\//i.test(value)
          ? 'github'
          : 'gitea';
      }
      return next;
    });
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
        // Local Git repos are defined only by their Local Root Folder
        // (gitea_url) — clear remote-only fields so stale values from a
        // previous provider choice are never persisted.
        ...(isLocal
          ? { clone_url: '', gitea_token: '', gitea_owner: '', gitea_repo: '' }
          : {}),
      };

      if (isLocal && !formData.gitea_url.trim()) {
        throw new Error('Local Root Folder is required for a Local Git repository');
      }

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
            <CCol md={4}>
              <CFormLabel>Name</CFormLabel>
              <CFormInput name="name" value={formData.name} onChange={handleChange} required />
            </CCol>
            <CCol md={2}>
              <CFormLabel>Provider</CFormLabel>
              <CFormSelect name="provider" value={formData.provider} onChange={handleChange}>
                <option value="gitea">Gitea</option>
                <option value="github">GitHub</option>
                <option value="local">Local Git</option>
              </CFormSelect>
            </CCol>
            <CCol md={6}>
              <CFormLabel>Clone URL{isLocal ? ' (not needed)' : ''}</CFormLabel>
              <CFormInput
                name="clone_url"
                value={isLocal ? '' : formData.clone_url}
                onChange={handleChange}
                placeholder={isLocal ? '' : 'https://github.com/owner/repo.git'}
                required={!isLocal}
                disabled={isLocal}
              />
            </CCol>
          </CRow>

          <CRow className="mb-3">
            <CCol md={isLocal ? 6 : 3}>
              <CFormLabel>
                {isLocal
                  ? 'Local Root Folder'
                  : formData.provider === 'github'
                    ? 'Host URL (optional)'
                    : 'Gitea URL'}
              </CFormLabel>
              <CFormInput
                name="gitea_url"
                value={formData.gitea_url}
                onChange={handleChange}
                placeholder={
                  isLocal
                    ? '/path/to/local/git/repo'
                    : formData.provider === 'github'
                      ? 'https://github.com'
                      : 'https://gitea.example.com'
                }
                required={isLocal}
              />
            </CCol>
            <CCol md={isLocal ? 2 : 3}>
              <CFormLabel>Owner{isLocal ? ' (not needed)' : ''}</CFormLabel>
              <CFormInput
                name="gitea_owner"
                value={isLocal ? '' : formData.gitea_owner}
                onChange={handleChange}
                placeholder={isLocal ? '' : 'org or user'}
                disabled={isLocal}
              />
            </CCol>
            <CCol md={isLocal ? 2 : 3}>
              <CFormLabel>Repo{isLocal ? ' (not needed)' : ''}</CFormLabel>
              <CFormInput
                name="gitea_repo"
                value={isLocal ? '' : formData.gitea_repo}
                onChange={handleChange}
                placeholder={isLocal ? '' : 'repository name'}
                disabled={isLocal}
              />
            </CCol>
            <CCol md={isLocal ? 2 : 3}>
              <CFormLabel>
                {isLocal
                  ? 'Token (not needed)'
                  : formData.provider === 'github'
                    ? 'GitHub Token'
                    : 'Gitea Token'}
              </CFormLabel>
              <CFormInput
                name="gitea_token"
                type="password"
                value={isLocal ? '' : formData.gitea_token}
                onChange={handleChange}
                placeholder={isLocal ? '' : formData.provider === 'github' ? 'ghp_… (repo scope)' : 'API token'}
                disabled={isLocal}
              />
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
