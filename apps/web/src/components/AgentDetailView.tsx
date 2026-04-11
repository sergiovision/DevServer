'use client';

import React, { useState, useEffect, useCallback } from 'react';
import { useRouter } from 'next/navigation';
import {
  CCard,
  CCardBody,
  CCardHeader,
  CBadge,
  CButton,
} from '@coreui/react-pro';
import { AgentLog } from './AgentLog';
import type { Task, TaskEvent } from '@/lib/types';

interface AgentDetailViewProps {
  task: Task;
  events: TaskEvent[];
}

export function AgentDetailView({ task, events: initialEvents }: AgentDetailViewProps) {
  const router = useRouter();
  const [events, setEvents] = useState<TaskEvent[]>(initialEvents);

  useEffect(() => {
    const wsUrl = process.env.NEXT_PUBLIC_WS_URL || 'ws://localhost:3000/api/ws';
    const ws = new WebSocket(wsUrl);

    ws.onopen = () => {
      ws.send(JSON.stringify({ type: 'subscribe', taskIds: [task.id] }));
    };

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        if (msg.type === 'task_event' && msg.taskId === task.id) {
          setEvents((prev) => [...prev, {
            id: Date.now(),
            task_id: msg.taskId,
            run_id: null,
            event_type: msg.eventType,
            payload: msg.payload,
            created_at: new Date().toISOString(),
          }]);
        }
      } catch {
        // ignore
      }
    };

    return () => { ws.close(); };
  }, [task.id]);

  const handleCancel = useCallback(async () => {
    await fetch(`/api/tasks/${task.id}/cancel`, { method: 'POST' });
    router.refresh();
  }, [task.id, router]);

  return (
    <>
      <div className="d-flex justify-content-between align-items-center mb-4">
        <div>
          <h2 className="mb-1">{task.task_key}</h2>
          <p className="text-body-secondary mb-0">{task.title}</p>
        </div>
        <div>
          <CBadge color="success" className="me-2 fs-6">RUNNING</CBadge>
          <CButton color="danger" className="me-2" onClick={handleCancel}>
            Cancel
          </CButton>
          <CButton color="outline-secondary" onClick={() => router.push('/agents')}>
            Back
          </CButton>
        </div>
      </div>

      <CCard>
        <CCardHeader>
          <strong>Live Log</strong>
          <span className="ms-2 text-body-secondary small">
            {events.length} events
          </span>
        </CCardHeader>
        <CCardBody>
          <AgentLog events={events} maxHeight="600px" />
        </CCardBody>
      </CCard>
    </>
  );
}
