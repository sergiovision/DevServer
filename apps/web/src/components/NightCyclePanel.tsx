'use client';

import { useEffect, useState, useCallback, useRef } from 'react';
import {
  CCard,
  CCardBody,
  CCardHeader,
  CBadge,
  CButton,
  CFormSelect,
  CSpinner,
} from '@coreui/react-pro';

interface NightCycleState {
  active: boolean;
  started_at?: string;
  end_time?: string;
  end_hour?: number;
  cycle_count?: number;
  current_task_id?: number | null;
  completed_task_ids?: number[];
  failed_task_ids?: number[];
  log?: string[];
  task_running?: boolean;
}

export function NightCyclePanel() {
  const [state, setState] = useState<NightCycleState | null>(null);
  const [endHour, setEndHour] = useState(7);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const inFlight = useRef<Promise<void> | null>(null);

  const fetchStatus = useCallback(async (force = false): Promise<void> => {
    // A slow worker must not stack background polls — but an explicit refresh
    // (after start/stop) waits for the in-flight poll and then fetches fresh
    // state instead of being silently skipped.
    if (inFlight.current && !force) return;
    while (inFlight.current) await inFlight.current.catch(() => {});
    const p = (async () => {
      await null; // let `inFlight.current = p` land before `finally` can clear it
      try {
        const res = await fetch('/api/night-cycle');
        if (res.ok) setState(await res.json());
      } catch {
      } finally {
        inFlight.current = null;
      }
    })();
    inFlight.current = p;
    return p;
  }, []);

  useEffect(() => {
    fetchStatus();
    // Poll only while the tab is visible; refresh immediately on return so
    // a backgrounded dashboard costs the worker nothing.
    const tick = () => {
      if (!document.hidden) fetchStatus();
    };
    const id = setInterval(tick, 15000);
    document.addEventListener('visibilitychange', tick);
    return () => {
      clearInterval(id);
      document.removeEventListener('visibilitychange', tick);
    };
  }, [fetchStatus]);

  const doAction = async (action: 'start' | 'stop') => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch('/api/night-cycle', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action, end_hour: endHour }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => null);
        const msg = data && (data.error || data.detail);
        setError(
          typeof msg === 'string' && msg ? msg : `Night cycle ${action} failed (HTTP ${res.status})`,
        );
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : `Night cycle ${action} failed`);
    } finally {
      await fetchStatus(true);
      setLoading(false);
    }
  };

  const isActive = state?.active;
  const completed = state?.completed_task_ids?.length ?? 0;
  const failed = state?.failed_task_ids?.length ?? 0;
  const log = state?.log ?? [];

  // UTC throughout — the picker and the worker's end_hour are both UTC.
  const endTimeStr = state?.end_time
    ? `${new Date(state.end_time).toLocaleTimeString([], {
        hour: '2-digit',
        minute: '2-digit',
        timeZone: 'UTC',
      })} UTC`
    : null;

  return (
    <CCard className="mb-4">
      <CCardHeader className="d-flex justify-content-between align-items-center">
        <div className="d-flex align-items-center gap-2">
          {!isActive && (
            <div className="d-flex align-items-center gap-1">
              <span className="text-body-secondary small">Until</span>
              <CFormSelect
                size="sm"
                style={{ width: 90 }}
                value={endHour}
                onChange={(e) => setEndHour(parseInt(e.target.value))}
              >
                {Array.from({ length: 12 }, (_, i) => i + 1).map((h) => (
                  <option key={h} value={h}>
                    {h.toString().padStart(2, '0')}:00
                  </option>
                ))}
              </CFormSelect>
              <span className="text-body-secondary small">UTC</span>
            </div>
          )}
          {isActive ? (
            <CButton
              color="danger"
              size="sm"
              disabled={loading}
              onClick={() => doAction('stop')}
            >
              {loading ? <CSpinner size="sm" /> : 'Stop'}
            </CButton>
          ) : (
            <CButton
              color="dark"
              size="sm"
              disabled={loading}
              onClick={() => doAction('start')}
            >
              {loading ? <CSpinner size="sm" /> : 'Start Night Cycle'}
            </CButton>
          )}
        </div>
        <div className="d-flex align-items-center gap-2">
          <strong>Night Cycle</strong>
          {isActive ? (
            <CBadge color="success">Active</CBadge>
          ) : (
            <CBadge color="secondary">Off</CBadge>
          )}
          {isActive && state?.cycle_count !== undefined && (
            <span className="text-body-secondary small">
              cycle {state.cycle_count} · {completed} passed · {failed} failed
              {endTimeStr && ` · until ${endTimeStr}`}
            </span>
          )}
        </div>
      </CCardHeader>

      {error && (
        <CCardBody className="p-2">
          <div className="text-danger small">{error}</div>
        </CCardBody>
      )}

      {isActive && log.length > 0 && (
        <CCardBody className="p-2">
          <pre
            className="mb-0 text-body-secondary"
            style={{ fontSize: 11, maxHeight: 160, overflowY: 'auto' }}
          >
            {log.slice(-20).join('\n')}
          </pre>
        </CCardBody>
      )}
    </CCard>
  );
}
