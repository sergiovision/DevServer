'use client';

import React, { useRef, useEffect } from 'react';
import type { TaskEvent } from '@/lib/types';

interface AgentLogProps {
  events: TaskEvent[];
  maxHeight?: string;
}

export function AgentLog({ events, maxHeight = '200px' }: AgentLogProps) {
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [events.length]);

  function formatTime(iso: string): string {
    try {
      return new Date(iso).toISOString().slice(11, 19);
    } catch {
      return '--:--:--';
    }
  }

  function formatEvent(event: TaskEvent): string {
    const payload = event.payload;
    if (typeof payload === 'object' && payload !== null) {
      if (payload.message) return String(payload.message);
      if (payload.line) return String(payload.line);
      if (payload.from && payload.to) return `Status: ${payload.from} -> ${payload.to}`;
      return JSON.stringify(payload);
    }
    return String(payload);
  }

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
      }}
    >
      {events.length === 0 && (
        <div className="text-secondary">Waiting for events...</div>
      )}
      {events.map((event) => (
        <div key={event.id} style={{ lineHeight: '1.4' }}>
          <span style={{ color: '#6a9955' }}>{formatTime(event.created_at)}</span>
          {' '}
          <span style={{ color: '#569cd6' }}>[{event.event_type}]</span>
          {' '}
          {formatEvent(event)}
        </div>
      ))}
      <div ref={bottomRef} />
    </div>
  );
}
