'use client';

/**
 * Sticky top banner that explains, once, why the dashboard is not working.
 *
 * Why it exists: without it an outage showed up as a stream of unrelated
 * error toasts from every panel. This component is the thin shell around the
 * pure state machine in `@/lib/connectivity`:
 *
 * - polls `GET /api/health/connectivity` (10 s while healthy, 3 s while
 *   degraded or while a failure awaits confirmation; paused while the tab is
 *   hidden, probed immediately when it becomes visible again);
 * - feeds browser `online` / `offline` events into the machine;
 * - publishes the status to the shared store, which `NotificationProvider`
 *   reads to suppress the toasts this banner already explains;
 * - on the restored transition flashes "Connection restored" and calls
 *   `emitConnectivityRestored()` so every `useResource` panel reloads.
 *
 * Renders nothing while online (apart from the brief restored flash).
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import {
  CONNECTIVITY_PROBE_URL,
  bannerMessage,
  initialConnectivityState,
  next,
  pollIntervalMs,
  setConnectivityStatus,
  type ConnectivityInput,
  type ConnectivityMachineState,
  type ConnectivityStatus,
} from '@/lib/connectivity';
import { emitConnectivityRestored } from '@/lib/connectivity-events';

const PROBE_TIMEOUT_MS = 5000;
const RESTORED_FLASH_MS = 3000;

const BANNER_COLORS: Record<Exclude<ConnectivityStatus, 'online'>, { bg: string; fg: string }> = {
  offline: { bg: '#495057', fg: '#ffffff' },
  'server-down': { bg: '#b02a37', fg: '#ffffff' },
  'worker-down': { bg: '#ffc107', fg: '#212529' },
};

async function probe(): Promise<ConnectivityInput> {
  try {
    const res = await fetch(CONNECTIVITY_PROBE_URL, {
      cache: 'no-store',
      signal: AbortSignal.timeout(PROBE_TIMEOUT_MS),
    });
    if (!res.ok) return { kind: 'probe-not-ok', httpStatus: res.status };
    const body: unknown = await res.json().catch(() => null);
    if (!body || typeof body !== 'object') return { kind: 'probe-not-ok', httpStatus: res.status };
    return { kind: 'probe-answered', worker: (body as { worker?: unknown }).worker === true };
  } catch {
    return { kind: 'probe-threw' };
  }
}

export function ConnectivityBanner() {
  const machine = useRef<ConnectivityMachineState>(initialConnectivityState());
  const [status, setStatus] = useState<ConnectivityStatus>('online');
  const [showRestored, setShowRestored] = useState(false);
  const pollTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const flashTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const probing = useRef(false);
  const mounted = useRef(true);

  const apply = useCallback((input: ConnectivityInput) => {
    const { state, restored } = next(machine.current, input);
    machine.current = state;
    setStatus(state.status);
    setConnectivityStatus(state.status);
    if (restored) {
      setShowRestored(true);
      clearTimeout(flashTimer.current);
      flashTimer.current = setTimeout(() => setShowRestored(false), RESTORED_FLASH_MS);
      emitConnectivityRestored();
    } else if (state.status !== 'online') {
      setShowRestored(false);
    }
  }, []);

  const probeNow = useCallback(async () => {
    clearTimeout(pollTimer.current);
    if (probing.current) return;
    if (typeof document !== 'undefined' && document.hidden) return; // resumed by visibilitychange
    probing.current = true;
    try {
      const input = await probe();
      if (mounted.current) apply(input);
    } finally {
      probing.current = false;
    }
    if (!mounted.current) return;
    pollTimer.current = setTimeout(() => void probeNow(), pollIntervalMs(machine.current));
  }, [apply]);

  useEffect(() => {
    mounted.current = true;
    machine.current = initialConnectivityState(navigator.onLine);
    setStatus(machine.current.status);
    setConnectivityStatus(machine.current.status);

    const handleOffline = () => apply({ kind: 'browser-offline' });
    const handleOnline = () => {
      apply({ kind: 'browser-online' });
      void probeNow();
    };
    const handleVisibility = () => {
      if (!document.hidden) void probeNow();
    };

    window.addEventListener('offline', handleOffline);
    window.addEventListener('online', handleOnline);
    document.addEventListener('visibilitychange', handleVisibility);
    void probeNow();

    return () => {
      mounted.current = false;
      window.removeEventListener('offline', handleOffline);
      window.removeEventListener('online', handleOnline);
      document.removeEventListener('visibilitychange', handleVisibility);
      clearTimeout(pollTimer.current);
      clearTimeout(flashTimer.current);
      setConnectivityStatus('online');
    };
  }, [apply, probeNow]);

  const message = bannerMessage(status);
  if (!message && !showRestored) return null;

  const colors = status === 'online' ? { bg: '#198754', fg: '#ffffff' } : BANNER_COLORS[status];

  return (
    <div
      role="status"
      aria-live="polite"
      className="connectivity-banner"
      style={{
        position: 'sticky',
        top: 0,
        zIndex: 1040,
        backgroundColor: colors.bg,
        color: colors.fg,
        padding: '6px 16px',
        fontSize: '0.85rem',
        fontWeight: 500,
        textAlign: 'center',
      }}
    >
      {message ?? 'Connection restored'}
    </div>
  );
}
