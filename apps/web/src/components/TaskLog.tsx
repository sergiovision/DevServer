'use client';

import React, { useEffect, useRef } from 'react';
import { api } from '@/lib/api-client';
import { useResource } from '@/lib/use-async';

interface TaskLogProps {
  taskKey: string;
  maxHeight?: string;
}

export function TaskLog({ taskKey, maxHeight = '600px' }: TaskLogProps) {
  const bottomRef = useRef<HTMLDivElement>(null);

  // Poll every 3 seconds; a failed poll keeps the last lines on screen.
  const { data: logLines = [] } = useResource<string[]>(
    async (signal) => {
      const data = await api.get<{ lines?: unknown }>(
        `/api/task-log/${encodeURIComponent(taskKey)}`,
        { signal },
      );
      return Array.isArray(data?.lines) ? (data.lines as string[]) : [];
    },
    [taskKey],
    { pollMs: 3000 },
  );

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [logLines.length]);

  return (
    <div
      className="font-monospace small"
      style={{
        maxHeight,
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
  );
}
