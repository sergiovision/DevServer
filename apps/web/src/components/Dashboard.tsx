'use client';

import React, { useState, useEffect, useCallback } from 'react';
import {
  CRow,
  CCol,
  CWidgetStatsA,
  CButton,
  CCard,
  CCardHeader,
  CCardBody,
  CBadge,
  CListGroup,
  CListGroupItem,
} from '@coreui/react-pro';
import CIcon from '@coreui/icons-react';
import { cilPeople, cilTask, cilCheckAlt, cilXCircle } from '@coreui/icons';
import { AgentCard } from './AgentCard';
import { WorkerToolbar } from './WorkerToolbar';
import { NightCyclePanel } from './pro-loader';
import type { Task, QueueStatsResponse } from '@/lib/types';
import { STATUS_COLORS } from '@/lib/types';

interface DashboardProps {
  runningTasks: Task[];
  queuedTasks: Task[];
  todayStats: { completed: number; failed: number; cost_usd: number };
}

export function Dashboard({ runningTasks, queuedTasks, todayStats }: DashboardProps) {
  const [running, setRunning] = useState(runningTasks);
  const [queued, setQueued] = useState(queuedTasks);
  const [stats, setStats] = useState(todayStats);
  const [queueStats, setQueueStats] = useState<QueueStatsResponse | null>(null);

  useEffect(() => {
    const wsUrl = process.env.NEXT_PUBLIC_WS_URL || 'ws://localhost:3000/api/ws';
    let ws: WebSocket | null = null;

    function connect() {
      ws = new WebSocket(wsUrl);
      ws.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data);
          if (msg.type === 'queue_update') {
            setQueueStats(msg.stats);
          }
        } catch {
          // ignore
        }
      };
      ws.onclose = () => {
        setTimeout(connect, 3000);
      };
    }
    connect();
    return () => { ws?.close(); };
  }, []);

  const handlePauseQueue = useCallback(async () => {
    await fetch('/api/queue/pause', { method: 'POST', body: JSON.stringify({ action: 'pause' }) });
  }, []);

  const handleResumeQueue = useCallback(async () => {
    await fetch('/api/queue/pause', { method: 'POST', body: JSON.stringify({ action: 'resume' }) });
  }, []);

  return (
    <>
      {/* Worker status + controls */}
      <WorkerToolbar />
      <NightCyclePanel />

      {/* Running Agents */}
      <CCard className="mb-4">
        <CCardHeader className="d-flex justify-content-between align-items-center">
          <strong>Running Agents ({running.length})</strong>
        </CCardHeader>
        <CCardBody>
          {running.length === 0 ? (
            <p className="text-body-secondary mb-0">No agents currently running.</p>
          ) : (
            running.map((task) => (
              <AgentCard key={task.id} task={task} />
            ))
          )}
        </CCardBody>
      </CCard>

      {/* Queue Backlog */}
      <CCard className="mb-4">
        <CCardHeader className="d-flex justify-content-between align-items-center">
          <div>
            <CButton color="primary" size="sm" className="me-2" href="/tasks/new">
              + Add Task
            </CButton>
            <CButton color="warning" size="sm" className="me-2" onClick={handlePauseQueue}>
              Pause Queue
            </CButton>
            <CButton color="success" size="sm" onClick={handleResumeQueue}>
              Resume Queue
            </CButton>
          </div>
          <strong>Backlog ({queued.length} pending)</strong>
        </CCardHeader>
        <CCardBody>
          {queued.length === 0 ? (
            <p className="text-body-secondary mb-0">Queue is empty.</p>
          ) : (
            <CListGroup>
              {queued.map((task) => (
                <CListGroupItem
                  key={task.id}
                  as="a"
                  href={`/tasks/${task.id}`}
                  className="d-flex justify-content-between align-items-center"
                >
                  <div>
                    <strong>{task.task_key}</strong>
                    <span className="ms-2 text-body-secondary">{task.title}</span>
                  </div>
                  <CBadge color={STATUS_COLORS[task.status]}>
                    {task.status}
                  </CBadge>
                </CListGroupItem>
              ))}
            </CListGroup>
          )}
        </CCardBody>
      </CCard>

      {/* Statistics */}
      <CRow>
        <CCol sm={6} lg={3}>
          <CWidgetStatsA
            className="mb-3"
            color="primary"
            value={
              <>{queueStats?.active ?? running.length} <span className="fs-6 fw-normal">running</span></>
            }
            title="Active Agents"
            action={<CIcon icon={cilPeople} height={36} />}
          />
        </CCol>
        <CCol sm={6} lg={3}>
          <CWidgetStatsA
            className="mb-3"
            color="info"
            value={
              <>{queueStats?.waiting ?? queued.length} <span className="fs-6 fw-normal">queued</span></>
            }
            title="Queue Backlog"
            action={<CIcon icon={cilTask} height={36} />}
          />
        </CCol>
        <CCol sm={6} lg={3}>
          <CWidgetStatsA
            className="mb-3"
            color="success"
            value={
              <>{stats.completed} <span className="fs-6 fw-normal">today</span></>
            }
            title="Completed"
            action={<CIcon icon={cilCheckAlt} height={36} />}
          />
        </CCol>
        <CCol sm={6} lg={3}>
          <CWidgetStatsA
            className="mb-3"
            color="danger"
            value={
              <>{stats.failed} <span className="fs-6 fw-normal">today</span></>
            }
            title="Failed"
            action={<CIcon icon={cilXCircle} height={36} />}
          />
        </CCol>
      </CRow>
    </>
  );
}
