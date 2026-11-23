'use client';

import React, { useState, useCallback } from 'react';
import {
  CButton,
  CCard,
  CCardHeader,
  CCardBody,
  CBadge,
  CListGroup,
  CListGroupItem,
} from '@coreui/react-pro';
import { AgentCard } from './AgentCard';
import { WorkerToolbar } from './WorkerToolbar';
import { NightCyclePanel } from './NightCyclePanel';
import { DashboardCharts } from './DashboardCharts';
import { DecisionsBanner } from './DecisionsBanner';
import type { Task } from '@/lib/types';
import { STATUS_COLORS } from '@/lib/types';
import { EmptyState } from '@/components/EmptyState';
import { api } from '@/lib/api-client';

interface DashboardProps {
  runningTasks: Task[];
  queuedTasks: Task[];
}

export function Dashboard({ runningTasks, queuedTasks }: DashboardProps) {
  const [running, setRunning] = useState(runningTasks);
  const [queued, setQueued] = useState(queuedTasks);

  const handlePauseQueue = useCallback(async () => {
    await api.post('/api/queue/pause', { action: 'pause' }).catch(() => undefined);
  }, []);

  const handleResumeQueue = useCallback(async () => {
    await api.post('/api/queue/pause', { action: 'resume' }).catch(() => undefined);
  }, []);

  return (
    <>
      {/* Worker status + controls */}
      <WorkerToolbar />
      <DecisionsBanner />
      <NightCyclePanel />

      {/* Running Agents */}
      <CCard className="mb-4">
        <CCardHeader className="d-flex justify-content-between align-items-center">
          <strong>Running Agents ({running.length})</strong>
        </CCardHeader>
        <CCardBody>
          {running.length === 0 ? (
            <EmptyState compact title="No agents currently running" />
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
            <EmptyState compact title="Queue is empty" />
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

      {/* Analytics Charts */}
      <DashboardCharts />
    </>
  );
}
