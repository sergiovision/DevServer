'use client';

import { useCallback, useEffect, useState } from 'react';
import { CButton, CCard, CCardBody, CCardHeader, CListGroup, CListGroupItem } from '@coreui/react-pro';

interface PatchFile {
  name: string;
  size_bytes: number;
}

interface PatchesPanelProps {
  taskId: number;
  /** Re-fetched when the status changes: patches appear when a run finishes. */
  taskStatus: string;
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/**
 * Downloadable `git format-patch` output of a patch-mode task. Patch mode
 * never pushes, so these files are how the change leaves the machine — e.g.
 * to be applied with `git am` on a repository the worker cannot reach.
 */
export function PatchesPanel({ taskId, taskStatus }: PatchesPanelProps) {
  const [patches, setPatches] = useState<PatchFile[] | null>(null);
  const [error, setError] = useState('');

  const loadPatches = useCallback(async () => {
    setError('');
    try {
      const res = await fetch(`/api/tasks/${taskId}/patches`);
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || 'Could not load patches');
      setPatches(Array.isArray(data.patches) ? data.patches : []);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load patches');
    }
  }, [taskId]);

  useEffect(() => {
    loadPatches();
  }, [loadPatches, taskStatus]);

  return (
    <CCard className="mb-4">
      <CCardHeader className="d-flex align-items-center">
        <strong>Patches</strong>
        <CButton color="link" size="sm" className="ms-auto p-0" onClick={loadPatches}>
          Refresh
        </CButton>
      </CCardHeader>
      <CCardBody className={patches && patches.length > 0 ? 'p-0' : undefined}>
        {error && <p className="text-danger small mb-0">{error}</p>}
        {!error && patches === null && <p className="text-body-secondary small mb-0">Loading…</p>}
        {!error && patches?.length === 0 && (
          <p className="text-body-secondary small mb-0">
            No patches yet. They are exported when a run of this task finishes.
          </p>
        )}
        {patches && patches.length > 0 && (
          <CListGroup flush>
            {patches.map((patch) => (
              <CListGroupItem key={patch.name} className="d-flex align-items-center gap-2">
                <a
                  href={`/api/tasks/${taskId}/patches/${encodeURIComponent(patch.name)}`}
                  download={patch.name}
                  className="text-break"
                >
                  {patch.name}
                </a>
                <small className="ms-auto text-body-secondary text-nowrap">
                  {formatSize(patch.size_bytes)}
                </small>
              </CListGroupItem>
            ))}
          </CListGroup>
        )}
      </CCardBody>
    </CCard>
  );
}
