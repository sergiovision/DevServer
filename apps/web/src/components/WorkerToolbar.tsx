'use client';

import React, { useState, useEffect, useCallback } from 'react';
import {
  CButton,
  CButtonGroup,
  CBadge,
  CSpinner,
  CCard,
  CCardBody,
  CRow,
  CCol,
} from '@coreui/react-pro';
import CIcon from '@coreui/icons-react';
import { cilReload, cilMediaPlay, cilMediaStop, cilSync } from '@coreui/icons';

interface WorkerStatus {
  running: boolean;
  pid?: string | null;
  status: {
    mode?: string;
    paused?: boolean;
    active_tasks?: { id: number; task_key: string; title: string }[];
    queued_tasks?: { id: number; task_key: string; title: string }[];
    counts?: { active: number; queued: number };
    worker_running?: boolean;
  } | null;
}

interface QueueStats {
  waiting: number;
  active: number;
  completed: number;
  failed: number;
}

export function WorkerToolbar() {
  const [worker, setWorker] = useState<WorkerStatus>({ running: false, status: null });
  const [queue, setQueue] = useState<QueueStats | null>(null);
  const [loading, setLoading] = useState(false);
  const [pendingAction, setPendingAction] = useState<'start' | 'restart' | 'stop' | null>(null);
  const [actionMsg, setActionMsg] = useState('');

  const refresh = useCallback(async () => {
    const [wRes, qRes] = await Promise.all([
      fetch('/api/worker').then((r) => r.json()).catch(() => ({ running: false, status: null })),
      fetch('/api/queue').then((r) => r.json()).catch(() => null),
    ]);
    setWorker(wRes);
    setQueue(qRes);
  }, []);

  useEffect(() => {
    refresh();
    const interval = setInterval(refresh, 10000);
    return () => clearInterval(interval);
  }, [refresh]);

  const doAction = useCallback(async (action: 'start' | 'restart' | 'stop') => {
    setLoading(true);
    setPendingAction(action);
    setActionMsg('');
    try {
      const res = await fetch('/api/worker', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action }),
      });
      const data = await res.json();
      if (data.success) {
        setActionMsg('');
        await refresh();
      } else {
        setActionMsg(`Failed: ${data.error}`);
      }
    } catch {
      setActionMsg('Request failed.');
    } finally {
      setLoading(false);
      setPendingAction(null);
    }
  }, [refresh]);

  const isStarting = loading && (pendingAction === 'start' || pendingAction === 'restart');
  const workerBadge = isStarting
    ? <CBadge color="warning">Worker starting…</CBadge>
    : worker.running
      ? <CBadge color="success">Online {worker.pid ? `(PID ${worker.pid})` : ''}</CBadge>
      : <CBadge color="danger">Offline</CBadge>;

  const paused = worker.status?.paused;

  return (
    <CCard className="mb-4">
      <CCardBody className="py-2">
        <CRow className="align-items-center g-2">

          {/* Worker status */}
          <CCol xs="auto">
            <span className="fw-semibold me-2">Worker</span>
            {workerBadge}
          </CCol>

          {/* Worker controls */}
          <CCol xs="auto">
            <CButtonGroup size="sm">
              {worker.running ? (
                <CButton
                  color="warning"
                  variant="outline"
                  disabled={loading}
                  onClick={() => doAction('restart')}
                  title="Restart worker"
                >
                  {loading ? <CSpinner size="sm" /> : <CIcon icon={cilReload} />}
                  {' '}Restart
                </CButton>
              ) : (
                <CButton
                  color="success"
                  variant="outline"
                  disabled={loading}
                  onClick={() => doAction('start')}
                  title="Start worker"
                >
                  {loading ? <CSpinner size="sm" /> : <CIcon icon={cilMediaPlay} />}
                  {' '}Start Worker
                </CButton>
              )}
              {worker.running && (
                <CButton
                  color="danger"
                  variant="outline"
                  disabled={loading}
                  onClick={() => doAction('stop')}
                  title="Stop worker"
                >
                  <CIcon icon={cilMediaStop} />
                  {' '}Stop
                </CButton>
              )}
            </CButtonGroup>
          </CCol>

          {paused && (
            <CCol xs="auto">
              <CBadge color="warning">Paused</CBadge>
            </CCol>
          )}

          {/* Queue stats */}
          {queue && (
            <>
              <CCol xs="auto" className="text-body-secondary">|</CCol>
              <CCol xs="auto">
                <span className="text-body-secondary small">Queue: </span>
                <CBadge color="info" className="me-1">{queue.active} active</CBadge>
                <CBadge color="secondary" className="me-1">{queue.waiting} waiting</CBadge>
                {queue.failed > 0 && (
                  <CBadge color="danger">{queue.failed} failed</CBadge>
                )}
              </CCol>
            </>
          )}

          {/* Active/queued tasks from worker */}
          {worker.status?.counts && (
            <>
              <CCol xs="auto" className="text-body-secondary">|</CCol>
              <CCol xs="auto">
                <span className="text-body-secondary small">Tasks: </span>
                <CBadge color="success" className="me-1">
                  {worker.status.counts.active} running
                </CBadge>
                <CBadge color="warning">
                  {worker.status.counts.queued} queued
                </CBadge>
              </CCol>
            </>
          )}

          {/* Refresh button */}
          <CCol xs="auto" className="ms-auto">
            {actionMsg && (
              <span className="text-body-secondary small me-3">{actionMsg}</span>
            )}
            <CButton size="sm" color="secondary" variant="ghost" onClick={refresh} title="Refresh">
              <CIcon icon={cilSync} />
            </CButton>
          </CCol>

        </CRow>
      </CCardBody>
    </CCard>
  );
}
