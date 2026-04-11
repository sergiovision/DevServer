'use client';

import { useCallback, useEffect, useState } from 'react';
import {
  CSmartTable,
  CBadge,
  CButton,
  CSpinner,
} from '@coreui/react-pro';

interface Job {
  name: string;
  group: string;
  schedule: string;
  is_running: boolean;
  prev_time: string | null;
  next_time: string | null;
  log: string;
}

const columns = [
  { key: 'is_running', label: 'Status', _style: { width: '110px' } },
  { key: 'name', label: 'Name' },
  { key: 'schedule', label: 'Schedule', _style: { width: '180px' } },
  { key: 'next_time', label: 'Next Run', _style: { width: '200px' } },
  { key: 'prev_time', label: 'Last Run', _style: { width: '200px' } },
  { key: 'log', label: 'Log' },
  { key: 'actions', label: '', _style: { width: '170px' }, sorter: false, filter: false },
];

function formatDate(iso: string | null): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return '—';
  return d.toLocaleString();
}

export function JobsView() {
  const [jobs, setJobs] = useState<Job[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await fetch('/api/jobs', { cache: 'no-store' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = (await res.json()) as Job[];
      setJobs(data);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 3000);
    return () => clearInterval(id);
  }, [load]);

  const run = useCallback(
    async (name: string) => {
      setBusy(name);
      try {
        await fetch('/api/jobs/run', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name }),
        });
        await load();
      } finally {
        setBusy(null);
      }
    },
    [load],
  );

  const stop = useCallback(
    async (name: string) => {
      setBusy(name);
      try {
        await fetch('/api/jobs/stop', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name }),
        });
        await load();
      } finally {
        setBusy(null);
      }
    },
    [load],
  );

  return (
    <>
      <div className="d-flex justify-content-between align-items-center mb-4">
        <CButton color="primary" onClick={load}>
          Refresh
        </CButton>
        <h2 className="mb-0">Jobs</h2>
      </div>

      {error && (
        <div className="alert alert-danger" role="alert">
          Failed to load jobs: {error}
        </div>
      )}

      {jobs === null && !error ? (
        <div className="text-center py-5">
          <CSpinner />
        </div>
      ) : (
        <CSmartTable
          items={jobs ?? []}
          columns={columns}
          tableProps={{ hover: true, responsive: true, striped: true }}
          columnSorter
          scopedColumns={{
            is_running: (item: Job) => (
              <td>
                {item.is_running ? (
                  <CBadge color="success">Running</CBadge>
                ) : (
                  <CBadge color="secondary">Idle</CBadge>
                )}
              </td>
            ),
            next_time: (item: Job) => (
              <td suppressHydrationWarning>{formatDate(item.next_time)}</td>
            ),
            prev_time: (item: Job) => (
              <td suppressHydrationWarning>{formatDate(item.prev_time)}</td>
            ),
            log: (item: Job) => (
              <td>
                <span className="text-body-secondary small">{item.log || '—'}</span>
              </td>
            ),
            actions: (item: Job) => (
              <td>
                <CButton
                  size="sm"
                  color="outline-success"
                  className="me-1"
                  disabled={busy === item.name || item.is_running}
                  onClick={() => run(item.name)}
                >
                  Run Now
                </CButton>
                <CButton
                  size="sm"
                  color="outline-danger"
                  disabled={busy === item.name || !item.is_running}
                  onClick={() => stop(item.name)}
                >
                  Stop
                </CButton>
              </td>
            ),
          }}
        />
      )}
    </>
  );
}
