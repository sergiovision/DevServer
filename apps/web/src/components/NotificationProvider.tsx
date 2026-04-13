'use client';

import React, { createContext, useContext, useState, useCallback, useEffect, useRef } from 'react';

type NotificationType = 'error' | 'warning' | 'info';

interface Notification {
  id: string;
  message: string;
  type: NotificationType;
}

interface NotificationContextValue {
  notify: (message: string, type?: NotificationType) => void;
}

const NotificationContext = createContext<NotificationContextValue>({
  notify: () => {},
});

export function useNotification() {
  return useContext(NotificationContext);
}

const BAR_COLORS: Record<NotificationType, string> = {
  error: '#dc3545',
  warning: '#fd7e14',
  info: '#0d6efd',
};

function statusMessage(status: number): string {
  if (status === 400) return 'Bad request (400)';
  if (status === 401) return 'Unauthorized (401)';
  if (status === 403) return 'Access denied (403)';
  if (status === 404) return 'Not found (404)';
  if (status === 408) return 'Request timeout (408)';
  if (status === 409) return 'Conflict (409)';
  if (status === 422) return 'Validation error (422)';
  if (status === 429) return 'Too many requests (429)';
  if (status === 500) return 'Internal server error (500)';
  if (status === 502) return 'Bad gateway (502)';
  if (status === 503) return 'Service unavailable (503)';
  if (status === 504) return 'Gateway timeout (504)';
  if (status >= 500) return `Server error (${status})`;
  if (status >= 400) return `Request failed (${status})`;
  return `Unexpected response (${status})`;
}

export function NotificationProvider({ children }: { children: React.ReactNode }) {
  const [stack, setStack] = useState<Notification[]>([]);
  const originalFetch = useRef<(typeof globalThis.fetch) | null>(null);
  const timers = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map());

  const dismiss = useCallback((id: string) => {
    clearTimeout(timers.current.get(id));
    timers.current.delete(id);
    setStack(prev => prev.filter(n => n.id !== id));
  }, []);

  const notify = useCallback((message: string, type: NotificationType = 'error') => {
    setStack(prev => {
      // Deduplicate: skip if the same message is already visible
      if (prev.some(n => n.message === message)) return prev;
      const id = Math.random().toString(36).slice(2);
      const timer = setTimeout(() => {
        timers.current.delete(id);
        setStack(s => s.filter(n => n.id !== id));
      }, 5000);
      timers.current.set(id, timer);
      return [...prev, { id, message, type }];
    });
  }, []);

  // Patch global fetch to intercept HTTP errors automatically
  useEffect(() => {
    originalFetch.current = window.fetch.bind(window);

    window.fetch = async function patchedFetch(...args: Parameters<typeof fetch>): Promise<Response> {
      const rawUrl =
        typeof args[0] === 'string'
          ? args[0]
          : args[0] instanceof URL
          ? args[0].toString()
          : (args[0] as Request).url;

      const isInternal =
        rawUrl.startsWith('/') || rawUrl.startsWith(window.location.origin);

      try {
        const response = await originalFetch.current!(...args);
        if (!response.ok && isInternal) {
          notify(statusMessage(response.status), 'error');
        }
        return response;
      } catch (err) {
        if (isInternal) {
          notify('Network error — server unreachable', 'error');
        }
        throw err;
      }
    };

    return () => {
      if (originalFetch.current) {
        window.fetch = originalFetch.current;
        originalFetch.current = null;
      }
    };
  }, [notify]);

  // Cleanup all timers on unmount
  useEffect(() => {
    const t = timers.current;
    return () => { t.forEach(clearTimeout); };
  }, []);

  const current = stack[stack.length - 1];

  return (
    <NotificationContext.Provider value={{ notify }}>
      {children}
      {current && (
        <div
          key={current.id}
          role="alert"
          aria-live="assertive"
          style={{
            position: 'fixed',
            bottom: 0,
            left: 0,
            right: 0,
            zIndex: 9999,
            backgroundColor: BAR_COLORS[current.type],
            color: '#fff',
            padding: '10px 16px',
            display: 'flex',
            alignItems: 'center',
            gap: 8,
            fontSize: '0.875rem',
            fontWeight: 500,
            boxShadow: '0 -2px 10px rgba(0,0,0,0.2)',
            animation: 'notif-slide-up 0.2s ease-out',
          }}
        >
          <span style={{ flex: 1 }}>{current.message}</span>
          {stack.length > 1 && (
            <span style={{ opacity: 0.75, fontSize: '0.8rem', whiteSpace: 'nowrap' }}>
              +{stack.length - 1} more
            </span>
          )}
          <button
            onClick={() => dismiss(current.id)}
            aria-label="Dismiss notification"
            style={{
              background: 'none',
              border: 'none',
              color: '#fff',
              cursor: 'pointer',
              fontSize: '1.1rem',
              lineHeight: 1,
              padding: '0 4px',
              opacity: 0.8,
              flexShrink: 0,
            }}
          >
            ×
          </button>
        </div>
      )}
    </NotificationContext.Provider>
  );
}
