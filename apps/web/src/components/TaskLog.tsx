'use client';

import React, { useState, useEffect, useRef } from 'react';

interface TaskLogProps {
  taskKey: string;
  maxHeight?: string;
}

export function TaskLog({ taskKey, maxHeight = '600px' }: TaskLogProps) {
  const [logLines, setLogLines] = useState<string[]>([]);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let active = true;
    const poll = async () => {
      try {
        const res = await fetch(`/api/task-log/${encodeURIComponent(taskKey)}`);
        if (res.ok) {
          const data = await res.json();
          if (active && Array.isArray(data.lines)) {
            setLogLines(data.lines);
          }
        }
      } catch { /* ignore */ }
    };
    poll();
    const interval = setInterval(poll, 3000);
    return () => { active = false; clearInterval(interval); };
  }, [taskKey]);

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
