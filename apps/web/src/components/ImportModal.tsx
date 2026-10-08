'use client';

/**
 * ImportModal — shared "Import from Confluence" dialog for the Tasks and
 * Ideas pages. Source selector → search (query/CQL + Space filter) →
 * results table with checkboxes → Markdown preview pane → target mapper
 * (Task: repo/priority/enqueue; Idea: root of the tree) → Import.
 *
 * Imports are idempotent: re-importing the same page updates the target
 * when the Confluence version changed and skips it otherwise (the
 * external_imports ledger, enforced server-side).
 */

import { useCallback, useEffect, useState } from 'react';
import {
  CAlert,
  CButton,
  CCol,
  CFormCheck,
  CFormInput,
  CFormLabel,
  CFormSelect,
  CFormSwitch,
  CModal,
  CModalBody,
  CModalFooter,
  CModalHeader,
  CModalTitle,
  CRow,
  CSpinner,
} from '@coreui/react-pro';

interface ImportSourceInfo {
  id: string;
  label: string;
  configured: boolean;
}

interface SearchItem {
  id: string;
  title: string;
  scope: string;
  version: string;
  updated: string;
  url: string;
}

interface Draft {
  external_id: string;
  title: string;
  markdown: string;
  scope: string;
  source_url: string;
  external_rev: string;
}

interface ImportResultRow {
  external_id: string;
  action: 'created' | 'updated' | 'skipped';
  target_type: string;
  target_id: number | null;
  task_key?: string;
  title: string;
}

interface RepoRow {
  id: number;
  name: string;
}

export interface ImportModalProps {
  visible: boolean;
  onClose: () => void;
  /** Which target the mapper preselects: 'task' (Tasks page) or 'idea' (Ideas page). */
  defaultTarget: 'task' | 'idea';
  /** Called after a successful import so the parent can refresh its data. */
  onImported?: () => void;
}

const ACTION_COLOR: Record<ImportResultRow['action'], string> = {
  created: 'success',
  updated: 'info',
  skipped: 'secondary',
};

export function ImportModal({ visible, onClose, defaultTarget, onImported }: ImportModalProps) {
  const [sources, setSources] = useState<ImportSourceInfo[]>([]);
  const [sourceId, setSourceId] = useState('confluence');
  const [repos, setRepos] = useState<RepoRow[]>([]);
  const [repoId, setRepoId] = useState<number | ''>('');
  const [scopes, setScopes] = useState<{ key: string; name: string }[]>([]);
  const [scope, setScope] = useState('');
  const [queryText, setQueryText] = useState('');
  const [searching, setSearching] = useState(false);
  const [items, setItems] = useState<SearchItem[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [preview, setPreview] = useState<Draft | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [targetType, setTargetType] = useState<'task' | 'idea'>(defaultTarget);
  const [priority, setPriority] = useState(3);
  const [enqueue, setEnqueue] = useState(true);
  const [importing, setImporting] = useState(false);
  const [results, setResults] = useState<ImportResultRow[] | null>(null);
  const [error, setError] = useState('');

  const repoQs = repoId ? `?repo_id=${repoId}` : '';

  // Reset transient state each time the modal opens.
  useEffect(() => {
    if (!visible) return;
    setTargetType(defaultTarget);
    setResults(null);
    setError('');
    setPreview(null);
    setSelected(new Set());
  }, [visible, defaultTarget]);

  // Load sources + repos once the modal opens.
  useEffect(() => {
    if (!visible) return;
    (async () => {
      try {
        const [srcRes, repoRes] = await Promise.all([
          fetch('/api/import/sources', { cache: 'no-store' }),
          fetch('/api/repos', { cache: 'no-store' }),
        ]);
        if (srcRes.ok) setSources(await srcRes.json());
        if (repoRes.ok) setRepos(await repoRes.json());
      } catch {
        setError('Failed to load import sources');
      }
    })();
  }, [visible]);

  // (Re)load spaces when the modal opens or the repo (creds override) changes.
  useEffect(() => {
    if (!visible) return;
    (async () => {
      try {
        const res = await fetch(`/api/import/confluence/scopes${repoQs}`, { cache: 'no-store' });
        const data = await res.json();
        setScopes(res.ok ? data.scopes || [] : []);
      } catch {
        setScopes([]);
      }
    })();
  }, [visible, repoQs]);

  const handleSearch = useCallback(async () => {
    setSearching(true);
    setError('');
    setItems([]);
    setSelected(new Set());
    setPreview(null);
    try {
      const params = new URLSearchParams({ q: queryText, limit: '25' });
      if (scope) params.set('scope', scope);
      if (repoId) params.set('repo_id', String(repoId));
      const res = await fetch(`/api/import/confluence/search?${params}`, { cache: 'no-store' });
      const data = await res.json();
      if (!res.ok) throw new Error(data?.detail || data?.error || `HTTP ${res.status}`);
      setItems(data.items || []);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSearching(false);
    }
  }, [queryText, scope, repoId]);

  const handlePreview = useCallback(async (id: string) => {
    setPreviewing(true);
    setPreview(null);
    try {
      const params = repoId ? `?repo_id=${repoId}` : '';
      const res = await fetch(`/api/import/confluence/item/${encodeURIComponent(id)}${params}`, {
        cache: 'no-store',
      });
      const data = await res.json();
      if (res.ok) setPreview(data);
    } finally {
      setPreviewing(false);
    }
  }, [repoId]);

  const toggleSelected = useCallback((id: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const handleImport = useCallback(async () => {
    setImporting(true);
    setError('');
    setResults(null);
    try {
      const res = await fetch('/api/import/confluence', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          ids: Array.from(selected),
          repo_id: repoId || null,
          target_type: targetType,
          task: { priority, enqueue },
          idea: { parent_id: null },
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data?.error || data?.detail || `HTTP ${res.status}`);
      setResults(data.results || []);
      if ((data.errors || []).length > 0) {
        setError(`${data.errors.length} page(s) failed to fetch`);
      }
      onImported?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setImporting(false);
    }
  }, [selected, repoId, targetType, priority, enqueue, onImported]);

  const source = sources.find((s) => s.id === sourceId);
  const canImport =
    selected.size > 0 && !importing && (targetType === 'idea' || Boolean(repoId));

  return (
    <CModal visible={visible} onClose={onClose} size="xl" scrollable>
      <CModalHeader>
        <CModalTitle>Import from {source?.label ?? 'Confluence'}</CModalTitle>
      </CModalHeader>
      <CModalBody>
        {error && <CAlert color="danger" className="py-2">{error}</CAlert>}
        {source && !source.configured && (
          <CAlert color="warning" className="py-2">
            Confluence is not configured — set the connection in Settings (or per-repo overrides) first.
          </CAlert>
        )}

        {/* Source / repo / space / query row */}
        <CRow className="mb-3 g-2">
          <CCol md={2}>
            <CFormLabel className="mb-1">Source</CFormLabel>
            <CFormSelect value={sourceId} onChange={(e) => setSourceId(e.target.value)}>
              {(sources.length ? sources : [{ id: 'confluence', label: 'Confluence', configured: false }]).map(
                (s) => (
                  <option key={s.id} value={s.id}>{s.label}</option>
                ),
              )}
            </CFormSelect>
          </CCol>
          <CCol md={3}>
            <CFormLabel className="mb-1">Repository{targetType === 'task' ? '' : ' (credentials)'}</CFormLabel>
            <CFormSelect
              value={String(repoId)}
              onChange={(e) => setRepoId(e.target.value ? parseInt(e.target.value) : '')}
            >
              <option value="">{targetType === 'task' ? 'Select repository…' : 'Global credentials'}</option>
              {repos.map((r) => (
                <option key={r.id} value={r.id}>{r.name}</option>
              ))}
            </CFormSelect>
          </CCol>
          <CCol md={3}>
            <CFormLabel className="mb-1">Space</CFormLabel>
            <CFormSelect value={scope} onChange={(e) => setScope(e.target.value)}>
              <option value="">All spaces</option>
              {scopes.map((s) => (
                <option key={s.key} value={s.key}>{s.name} ({s.key})</option>
              ))}
            </CFormSelect>
          </CCol>
          <CCol md={4}>
            <CFormLabel className="mb-1">Search (or &quot;cql:&hellip;&quot;)</CFormLabel>
            <div className="d-flex gap-2">
              <CFormInput
                placeholder="Search pages…"
                value={queryText}
                onChange={(e) => setQueryText(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); handleSearch(); } }}
              />
              <CButton color="primary" onClick={handleSearch} disabled={searching}>
                {searching ? <CSpinner size="sm" /> : 'Search'}
              </CButton>
            </div>
          </CCol>
        </CRow>

        {/* Results + preview panes */}
        <CRow className="mb-3">
          <CCol md={preview || previewing ? 6 : 12}>
            {items.length === 0 && !searching ? (
              <div className="text-body-secondary small py-3">
                No results — search Confluence pages above.
              </div>
            ) : (
              <div style={{ maxHeight: 320, overflowY: 'auto' }}>
                <table className="table table-sm table-hover align-middle mb-0">
                  <thead>
                    <tr>
                      <th style={{ width: 28 }} />
                      <th>Title</th>
                      <th>Space</th>
                      <th>v</th>
                    </tr>
                  </thead>
                  <tbody>
                    {items.map((it) => (
                      <tr
                        key={it.id}
                        onClick={() => handlePreview(it.id)}
                        style={{ cursor: 'pointer' }}
                        className={preview?.external_id === it.id ? 'table-active' : undefined}
                      >
                        <td onClick={(e) => e.stopPropagation()}>
                          <CFormCheck
                            checked={selected.has(it.id)}
                            onChange={() => toggleSelected(it.id)}
                            aria-label={`Select ${it.title}`}
                          />
                        </td>
                        <td>{it.title}</td>
                        <td><span className="badge text-bg-light">{it.scope}</span></td>
                        <td className="text-body-secondary">{it.version}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </CCol>
          {(preview || previewing) && (
            <CCol md={6}>
              <div className="border rounded p-2" style={{ maxHeight: 320, overflowY: 'auto' }}>
                {previewing ? (
                  <div className="text-center py-4"><CSpinner size="sm" /></div>
                ) : preview ? (
                  <>
                    <div className="fw-semibold mb-1">
                      {preview.title}{' '}
                      <small className="text-body-secondary">v{preview.external_rev}</small>
                    </div>
                    <pre className="small mb-0" style={{ whiteSpace: 'pre-wrap' }}>
                      {preview.markdown}
                    </pre>
                  </>
                ) : null}
              </div>
            </CCol>
          )}
        </CRow>

        {/* Target mapper */}
        <CRow className="g-2 align-items-end">
          <CCol md={3}>
            <CFormLabel className="mb-1">Import as</CFormLabel>
            <CFormSelect
              value={targetType}
              onChange={(e) => setTargetType(e.target.value as 'task' | 'idea')}
            >
              <option value="task">Task (enqueued)</option>
              <option value="idea">Idea</option>
            </CFormSelect>
          </CCol>
          {targetType === 'task' && (
            <>
              <CCol md={3}>
                <CFormLabel className="mb-1">Priority</CFormLabel>
                <CFormSelect
                  value={String(priority)}
                  onChange={(e) => setPriority(parseInt(e.target.value))}
                >
                  <option value="1">1 — Critical</option>
                  <option value="2">2 — High</option>
                  <option value="3">3 — Medium</option>
                  <option value="4">4 — Low</option>
                </CFormSelect>
              </CCol>
              <CCol md={3}>
                <CFormSwitch
                  label="Enqueue immediately"
                  checked={enqueue}
                  onChange={(e) => setEnqueue(e.target.checked)}
                />
              </CCol>
            </>
          )}
        </CRow>

        {/* Per-item results after an import */}
        {results && (
          <div className="mt-3">
            {results.length === 0 ? (
              <CAlert color="warning" className="py-2 mb-0">Nothing imported.</CAlert>
            ) : (
              <ul className="list-unstyled small mb-0">
                {results.map((r) => (
                  <li key={`${r.external_id}-${r.action}`}>
                    <span className={`badge text-bg-${ACTION_COLOR[r.action]} me-2`}>{r.action}</span>
                    {r.title}
                    {r.task_key && <code className="ms-2">{r.task_key}</code>}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </CModalBody>
      <CModalFooter>
        <span className="me-auto small text-body-secondary">
          {selected.size} page{selected.size === 1 ? '' : 's'} selected
          {targetType === 'task' && !repoId ? ' — select a repository to import as tasks' : ''}
        </span>
        <CButton color="secondary" variant="outline" onClick={onClose}>Close</CButton>
        <CButton color="primary" onClick={handleImport} disabled={!canImport}>
          {importing ? 'Importing…' : `Import ${selected.size || ''}`.trim()}
        </CButton>
      </CModalFooter>
    </CModal>
  );
}
