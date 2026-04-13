'use client';

import React, {
  useState,
  useEffect,
  useRef,
  useCallback,
  useLayoutEffect,
} from 'react';
import {
  CNav,
  CNavItem,
  CNavLink,
  CTabContent,
  CTabPane,
  CCard,
  CCardBody,
  CButton,
  CBadge,
  CSpinner,
} from '@coreui/react-pro';
import CIcon from '@coreui/icons-react';
import { cilArrowBottom, cilTrash, cilSync } from '@coreui/icons';

// ─── Types ───────────────────────────────────────────────────────────────────

type LogName = 'worker' | 'web';

interface LogState {
  lines: string[];
  offset: number;
  loading: boolean;
  error: string | null;
  lastUpdated: Date | null;
}

const INITIAL: LogState = {
  lines: [],
  offset: 0,
  loading: true,
  error: null,
  lastUpdated: null,
};

const POLL_MS = 1500;

// ─── Hook ─────────────────────────────────────────────────────────────────────

function useLogTail(name: LogName, active: boolean) {
  const [state, setState] = useState<LogState>(INITIAL);
  const offsetRef = useRef(0);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const fetchLines = useCallback(
    async (reset = false) => {
      const since = reset ? 0 : offsetRef.current;
      try {
        const res = await fetch(`/api/logs/${name}?since=${since}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data: { lines: string[]; nextOffset: number } = await res.json();

        offsetRef.current = data.nextOffset;

        setState(prev => ({
          lines: reset ? data.lines : [...prev.lines, ...data.lines],
          offset: data.nextOffset,
          loading: false,
          error: null,
          lastUpdated: data.lines.length > 0 || reset ? new Date() : prev.lastUpdated,
        }));
      } catch (err) {
        setState(prev => ({
          ...prev,
          loading: false,
          error: err instanceof Error ? err.message : 'Failed to fetch log',
        }));
      }
    },
    [name],
  );

  // Reset + initial load whenever this tab becomes active
  useEffect(() => {
    if (!active) return;
    setState({ ...INITIAL });
    offsetRef.current = 0;
    fetchLines(true);
  }, [active, fetchLines]);

  // Polling while active
  useEffect(() => {
    if (!active) return;
    intervalRef.current = setInterval(() => fetchLines(false), POLL_MS);
    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current);
    };
  }, [active, fetchLines]);

  const clear = useCallback(() => {
    setState(prev => ({ ...prev, lines: [] }));
  }, []);

  const refresh = useCallback(() => {
    setState({ ...INITIAL });
    offsetRef.current = 0;
    fetchLines(true);
  }, [fetchLines]);

  return { ...state, clear, refresh };
}

// ─── Log Panel ────────────────────────────────────────────────────────────────

interface LogPanelProps {
  name: LogName;
  active: boolean;
}

function LogPanel({ name, active }: LogPanelProps) {
  const { lines, loading, error, lastUpdated, clear, refresh } = useLogTail(name, active);
  const containerRef = useRef<HTMLPreElement>(null);
  const [atBottom, setAtBottom] = useState(true);
  const userScrolledRef = useRef(false);

  // Auto-scroll to bottom when new lines arrive, unless user has scrolled up
  useLayoutEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    if (atBottom) {
      el.scrollTop = el.scrollHeight;
    }
  }, [lines, atBottom]);

  const handleScroll = useCallback(() => {
    const el = containerRef.current;
    if (!el) return;
    const threshold = 40;
    const isNearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < threshold;
    setAtBottom(isNearBottom);
    userScrolledRef.current = !isNearBottom;
  }, []);

  const scrollToBottom = useCallback(() => {
    const el = containerRef.current;
    if (el) {
      el.scrollTop = el.scrollHeight;
      setAtBottom(true);
    }
  }, []);

  const lastUpdatedStr = lastUpdated
    ? lastUpdated.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
    : '—';

  return (
    <div className="d-flex flex-column" style={{ height: 'calc(100vh - 260px)', minHeight: 400 }}>
      {/* Toolbar */}
      <div className="d-flex align-items-center gap-2 mb-2 flex-wrap">
        {/* Live indicator */}
        <span className="d-flex align-items-center gap-1">
          <span
            style={{
              display: 'inline-block',
              width: 8,
              height: 8,
              borderRadius: '50%',
              backgroundColor: error ? '#dc3545' : '#198754',
              boxShadow: error ? 'none' : '0 0 0 2px rgba(25,135,84,0.3)',
              animation: error ? 'none' : 'log-pulse 2s ease-in-out infinite',
            }}
          />
          <span className="text-body-secondary small">{error ? 'Error' : 'Live'}</span>
        </span>

        <span className="text-body-secondary small">
          {lines.length} line{lines.length !== 1 ? 's' : ''}
        </span>

        <span className="text-body-secondary small ms-1">
          Updated: {lastUpdatedStr}
        </span>

        <div className="ms-auto d-flex gap-2">
          <CButton size="sm" color="secondary" variant="ghost" onClick={refresh} title="Reload from start">
            <CIcon icon={cilSync} />
          </CButton>
          <CButton size="sm" color="secondary" variant="ghost" onClick={clear} title="Clear display">
            <CIcon icon={cilTrash} />
          </CButton>
        </div>
      </div>

      {/* Log output */}
      <div className="position-relative flex-grow-1" style={{ minHeight: 0 }}>
        {loading && lines.length === 0 && (
          <div className="d-flex align-items-center justify-content-center h-100">
            <CSpinner size="sm" className="me-2" />
            <span className="text-body-secondary small">Loading…</span>
          </div>
        )}

        {!loading && lines.length === 0 && !error && (
          <div className="d-flex align-items-center justify-content-center h-100">
            <span className="text-body-secondary small">
              No log output yet — file may not exist.
            </span>
          </div>
        )}

        {error && lines.length === 0 && (
          <div className="d-flex align-items-center justify-content-center h-100">
            <span className="text-danger small">{error}</span>
          </div>
        )}

        <pre
          ref={containerRef}
          onScroll={handleScroll}
          style={{
            height: '100%',
            overflowY: 'auto',
            margin: 0,
            padding: '10px 14px',
            fontSize: '0.78rem',
            lineHeight: 1.55,
            fontFamily: 'ui-monospace, "SFMono-Regular", "Cascadia Code", Menlo, monospace',
            backgroundColor: 'var(--cui-dark, #1a1d23)',
            color: '#d4d4d4',
            borderRadius: 6,
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-all',
            display: lines.length === 0 && (loading || !error) ? 'none' : 'block',
          }}
        >
          {lines.map((line, i) => (
            <LogLine key={i} line={line} />
          ))}
        </pre>

        {/* Jump-to-bottom FAB */}
        {!atBottom && (
          <button
            onClick={scrollToBottom}
            title="Jump to bottom"
            style={{
              position: 'absolute',
              bottom: 12,
              right: 18,
              background: 'rgba(13,110,253,0.85)',
              border: 'none',
              borderRadius: '50%',
              width: 32,
              height: 32,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              cursor: 'pointer',
              color: '#fff',
              boxShadow: '0 2px 6px rgba(0,0,0,0.3)',
            }}
          >
            <CIcon icon={cilArrowBottom} size="sm" />
          </button>
        )}
      </div>
    </div>
  );
}

// ─── Log Line (colourised) ────────────────────────────────────────────────────

function logLineColor(line: string): string {
  const u = line.toUpperCase();
  if (u.includes('[ERROR]') || u.includes('ERROR:') || u.includes('CRITICAL')) return '#f48771';
  if (u.includes('[WARNING]') || u.includes('[WARN]') || u.includes('WARNING:')) return '#dcdcaa';
  if (u.includes('[INFO]') || u.includes('INFO:')) return '#9cdcfe';
  if (u.includes('[DEBUG]') || u.includes('DEBUG:')) return '#6a9955';
  return '#d4d4d4';
}

function LogLine({ line }: { line: string }) {
  return (
    <span style={{ color: logLineColor(line), display: 'block' }}>
      {line}
      {'\n'}
    </span>
  );
}

// ─── Main view ────────────────────────────────────────────────────────────────

const TABS: { key: LogName; label: string }[] = [
  { key: 'worker', label: 'worker.log' },
  { key: 'web',    label: 'web.log' },
];

export function LogsView() {
  const [activeTab, setActiveTab] = useState<LogName>('worker');

  return (
    <>
      <h2 className="mb-4">Logs</h2>
      <CCard>
        <CCardBody>
          <CNav variant="tabs" className="mb-3">
            {TABS.map(t => (
              <CNavItem key={t.key}>
                <CNavLink
                  active={activeTab === t.key}
                  onClick={() => setActiveTab(t.key)}
                  style={{ cursor: 'pointer' }}
                >
                  <code style={{ fontSize: '0.85rem' }}>{t.label}</code>
                </CNavLink>
              </CNavItem>
            ))}
          </CNav>

          <CTabContent>
            {TABS.map(t => (
              <CTabPane key={t.key} visible={activeTab === t.key}>
                <LogPanel name={t.key} active={activeTab === t.key} />
              </CTabPane>
            ))}
          </CTabContent>
        </CCardBody>
      </CCard>

      {/* pulse animation for the live indicator dot */}
      <style>{`
        @keyframes log-pulse {
          0%, 100% { opacity: 1; }
          50%       { opacity: 0.4; }
        }
      `}</style>
    </>
  );
}
