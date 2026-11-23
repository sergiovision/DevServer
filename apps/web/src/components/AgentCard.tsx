'use client';

import React, { useState, useEffect, useRef } from 'react';
import {
  CCard,
  CCardBody,
  CCardHeader,
  CProgress,
  CBadge,
  CButton,
} from '@coreui/react-pro';
import type { Task } from '@/lib/types';
import { api } from '@/lib/api-client';
import { useResource } from '@/lib/use-async';

interface AgentCardProps {
  task: Task;
}

export function AgentCard({ task }: AgentCardProps) {
  const [expanded, setExpanded] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);

  // Poll the task log file every 3 seconds. A failed poll keeps the last
  // lines on screen (useResource keeps `data` when a reload errors).
  const { data: logLines = [] } = useResource<string[]>(
    async (signal) => {
      const data = await api.get<{ lines?: unknown }>(
        `/api/task-log/${encodeURIComponent(task.task_key)}`,
        { signal },
      );
      return Array.isArray(data?.lines) ? (data.lines as string[]) : [];
    },
    [task.task_key],
    { pollMs: 3000 },
  );

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [logLines.length]);

  const handleCancel = async () => {
    await api.post(`/api/tasks/${task.id}/cancel`).catch(() => undefined);
  };

  return (
    <CCard className="mb-3">
      <CCardHeader className="d-flex justify-content-between align-items-center">
        <div>
          <strong>{task.task_key}</strong>
          <CBadge color="success" className="ms-2">RUNNING</CBadge>
          {task.repo_name && (
            <span className="ms-2 text-body-secondary small">{task.repo_name}</span>
          )}
        </div>
        <div>
          <CButton
            size="sm"
            color="outline-secondary"
            className="me-1"
            onClick={() => setExpanded(!expanded)}
          >
            {expanded ? 'Collapse' : 'Expand'}
          </CButton>
          <CButton size="sm" color="outline-danger" onClick={handleCancel}>
            Cancel
          </CButton>
        </div>
      </CCardHeader>
      <CCardBody>
        <p className="mb-2 text-body-secondary small">{task.title}</p>
        <CProgress className="mb-2" value={0} animated color="success" />
        <div
          className="font-monospace small"
          style={{
            maxHeight: expanded ? '400px' : '150px',
            overflowY: 'auto',
            backgroundColor: '#1e1e1e',
            color: '#d4d4d4',
            borderRadius: '4px',
            padding: '8px',
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-all',
          }}
        >
          {logLines.length === 0 ? (
            <div className="text-secondary">Waiting for log output...</div>
          ) : (
            logLines.map((line, i) => (
              <div key={i} style={{ lineHeight: '1.4' }}>{line}</div>
            ))
          )}
          <div ref={bottomRef} />
        </div>
      </CCardBody>
    </CCard>
  );
}
