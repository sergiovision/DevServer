/**
 * PatchesPanel — download the task's agent-branch changes as git patches.
 *
 * Renders in the TaskDetail sidebar. Calls the /api/task-patches proxy
 * routes, not the worker directly. Two user actions:
 *
 *   1. **Generate / Regenerate** — POST triggers format-patch on the bare repo.
 *   2. **Download** — single file or the combined .mbox (preferred — that's
 *      what `git am < combined.mbox` wants on the receiving side).
 *
 * Also shows a copy-to-clipboard box with the exact command the operator
 * should run on the production repo, so there's no guessing.
 */
'use client';

import React, { useCallback, useEffect, useState } from 'react';
import {
  CAlert,
  CBadge,
  CButton,
  CCard,
  CCardBody,
  CCardHeader,
  CSpinner,
} from '@coreui/react-pro';

interface PatchFile {
  filename: string;
  size_bytes: number;
  kind: 'mbox';
}

interface PatchSet {
  ok: boolean;
  task_key: string;
  directory: string | null;
  base_branch: string;
  branch_name: string;
  files: PatchFile[];
  commits: number;
  files_changed: number;
  insertions: number;
  deletions: number;
  generated_at: string | null;
  error: string | null;
}

interface PatchesPanelProps {
  taskKey: string;
  taskStatus: string;
}

const EMPTY_PATCHSET: PatchSet = {
  ok: false,
  task_key: '',
  directory: null,
  base_branch: '',
  branch_name: '',
  files: [],
  commits: 0,
  files_changed: 0,
  insertions: 0,
  deletions: 0,
  generated_at: null,
  error: null,
};

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatDate(iso: string | null): string {
  if (!iso) return 'never';
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

export function PatchesPanel({ taskKey, taskStatus }: PatchesPanelProps) {
  const [patchset, setPatchset] = useState<PatchSet>(EMPTY_PATCHSET);
  const [loading, setLoading] = useState(true);
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState<string>('');
  const [copied, setCopied] = useState(false);

  const load = useCallback(async () => {
    setError('');
    try {
      const res = await fetch(`/api/task-patches/${encodeURIComponent(taskKey)}`, {
        cache: 'no-store',
      });
      if (res.ok) {
        const data = await res.json();
        setPatchset({ ...EMPTY_PATCHSET, ...data });
      } else {
        const data = await res.json().catch(() => null);
        setError(data?.error ?? `Worker returned ${res.status}`);
      }
    } catch {
      setError('Worker unreachable');
    } finally {
      setLoading(false);
    }
  }, [taskKey]);

  useEffect(() => {
    load();
  }, [load]);

  const handleGenerate = useCallback(async () => {
    setGenerating(true);
    setError('');
    try {
      const res = await fetch(`/api/task-patches/${encodeURIComponent(taskKey)}`, {
        method: 'POST',
      });
      const data = await res.json().catch(() => null);
      if (!res.ok) {
        setError(data?.error ?? `Worker returned ${res.status}`);
      } else {
        setPatchset({ ...EMPTY_PATCHSET, ...data });
      }
    } catch {
      setError('Worker unreachable');
    } finally {
      setGenerating(false);
    }
  }, [taskKey]);

  const copyCommand = useCallback(async () => {
    const cmd = 'git am < combined.mbox';
    try {
      await navigator.clipboard.writeText(cmd);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      /* clipboard blocked — nothing to do */
    }
  }, []);

  const mbox = patchset.files.find((f) => f.kind === 'mbox');

  const canGenerate = !generating && taskStatus !== 'running' && taskStatus !== 'verifying';

  const hasAnything = patchset.files.length > 0;

  return (
    <CCard className="mb-4">
      <CCardHeader className="d-flex align-items-center justify-content-between">
        <strong>Patches</strong>
        <CButton
          color="primary"
          size="sm"
          disabled={!canGenerate}
          onClick={handleGenerate}
          title={
            !canGenerate
              ? 'Cannot regenerate while the task is running or verifying'
              : 'Regenerate patches from the agent branch'
          }
        >
          {generating ? (
            <>
              <CSpinner size="sm" className="me-1" />
              Generating…
            </>
          ) : hasAnything ? (
            'Regenerate'
          ) : (
            'Generate'
          )}
        </CButton>
      </CCardHeader>
      <CCardBody>
        {error && (
          <CAlert color="danger" className="py-2 mb-3">
            {error}
          </CAlert>
        )}

        {loading ? (
          <div className="text-body-secondary">Loading…</div>
        ) : !hasAnything ? (
          <div className="text-body-secondary small">
            No patches yet. Patches are auto-generated when a task finishes
            successfully, or you can click <strong>Generate</strong> above to
            build them on demand.
          </div>
        ) : (
          <>
            {/* Summary row */}
            <div className="mb-3 small text-body-secondary">
              <div>
                <CBadge color="info" className="me-2">
                  {patchset.commits} commit{patchset.commits === 1 ? '' : 's'}
                </CBadge>
                <CBadge color="success" className="me-1">
                  +{patchset.insertions}
                </CBadge>
                <CBadge color="danger" className="me-2">
                  −{patchset.deletions}
                </CBadge>
                <span>
                  across {patchset.files_changed} file
                  {patchset.files_changed === 1 ? '' : 's'}
                </span>
              </div>
              <div className="mt-1">
                Generated <span suppressHydrationWarning>{formatDate(patchset.generated_at)}</span>
              </div>
            </div>

            {/* Combined mbox — the important one, highlighted */}
            {mbox && (
              <div className="mb-3">
                <a
                  href={`/api/task-patches/${encodeURIComponent(taskKey)}/file/${encodeURIComponent(mbox.filename)}`}
                  download={mbox.filename}
                  className="btn btn-success btn-lg w-100"
                >
                  ⬇ Download <strong>combined.mbox</strong>
                  <span className="ms-2 small opacity-75">
                    ({formatBytes(mbox.size_bytes)})
                  </span>
                </a>
                <div className="small text-body-secondary mt-2">
                  On the target repo, run:
                </div>
                <div className="d-flex align-items-center gap-2 mt-1">
                  <code
                    className="flex-grow-1 px-2 py-1 bg-body-tertiary rounded small font-monospace"
                    style={{ overflow: 'auto' }}
                  >
                    git am &lt; combined.mbox
                  </code>
                  <CButton
                    size="sm"
                    color="secondary"
                    variant="outline"
                    onClick={copyCommand}
                  >
                    {copied ? 'Copied!' : 'Copy'}
                  </CButton>
                </div>
              </div>
            )}

          </>
        )}
      </CCardBody>
    </CCard>
  );
}
