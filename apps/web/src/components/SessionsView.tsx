'use client';

/**
 * Claude and OpenAI Codex session observatory.
 *
 * Watches every Claude Code session on the worker host and replays its
 * transcript. Mechanically this is the Logs page — the same byte-offset tail,
 * the same 1.5s poll, the same auto-scroll — but a session is a conversation
 * rather than a line stream, so it renders as a transcript: user/assistant
 * turns, thinking dimmed, tool calls collapsed to one row you can expand.
 *
 * Master-detail rather than the Logs page's tabs: there are two log files on a
 * host and there can be forty sessions.
 */

import React, {
  useState,
  useEffect,
  useRef,
  useCallback,
  useMemo,
  useLayoutEffect,
} from 'react';
import Link from 'next/link';
import {
  CCard,
  CCardBody,
  CButton,
  CBadge,
  CSpinner,
  CFormInput,
  CFormCheck,
  CButtonGroup,
} from '@coreui/react-pro';
import CIcon from '@coreui/icons-react';
import { cilArrowBottom, cilCheckAlt, cilCopy, cilSync, cilTrash, cilTerminal } from '@coreui/icons';
import { AskAgentMarkdown } from './AskAgentMarkdown';

// ─── Types (mirror services/claude_sessions.py) ──────────────────────────

export interface SessionRow {
  session_id: string;
  cwd: string;
  label: string;
  git_branch: string | null;
  version: string | null;
  entrypoint: string | null;
  kind: string;
  pid: number | null;
  status: string;       // 'busy' | 'idle' | 'active' | 'ended'
  live: boolean;
  started_at: string | null;
  last_activity: number;
  idle_seconds: number;
  size_bytes: number;
  task_id: number | null;
  task_key: string | null;
  task_status: string | null;
  repo_id: number | null;
  repo_name: string | null;
  origin: string;       // 'devserver' | 'assistant' | 'console' | 'external'
}

interface Ev {
  type: 'text' | 'thinking' | 'tool_call' | 'tool_result' | 'usage' | 'notice';
  ts?: string;
  sidechain?: boolean;
  // text / thinking / notice
  role?: 'user' | 'assistant';
  text?: string;
  truncated?: boolean;
  redacted?: string[] | null;
  model?: string;
  // tool_call
  id?: string;
  name?: string;
  summary?: string;
  // tool_result
  tool_use_id?: string;
  lines?: number;
  is_error?: boolean;
  // usage
  msg_id?: string;
  input_tokens?: number;
  output_tokens?: number;
  cache_read_tokens?: number;
  cache_write_tokens?: number;
}

interface ApiError {
  error: string;
  hint?: string;
}

const LIST_POLL_MS = 5000;
const TAIL_POLL_MS = 1500;
type SessionProvider = 'claude' | 'codex';

/** Anthropic's Claude sunburst, in the brand orange. */
function ClaudeIcon({ size = 16 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" aria-hidden="true">
      <g stroke="#D97757" strokeWidth="1.7" strokeLinecap="round">
        <line x1="12" y1="12" x2="12" y2="2.8" />
        <line x1="12" y1="12" x2="15" y2="6.8" />
        <line x1="12" y1="12" x2="19.97" y2="7.4" />
        <line x1="12" y1="12" x2="18" y2="12" />
        <line x1="12" y1="12" x2="19.97" y2="16.6" />
        <line x1="12" y1="12" x2="15" y2="17.2" />
        <line x1="12" y1="12" x2="12" y2="21.2" />
        <line x1="12" y1="12" x2="9" y2="17.2" />
        <line x1="12" y1="12" x2="4.03" y2="16.6" />
        <line x1="12" y1="12" x2="6" y2="12" />
        <line x1="12" y1="12" x2="4.03" y2="7.4" />
        <line x1="12" y1="12" x2="9" y2="6.8" />
      </g>
    </svg>
  );
}

/**
 * The OpenAI knot, monochrome — it inherits `currentColor` so it stays legible
 * against both the active (filled) and inactive (outline) tab.
 */
function ChatGptIcon({ size = 16 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="currentColor"
      aria-hidden="true"
    >
      <path d="M22.2819 9.8211a5.9847 5.9847 0 0 0-.5157-4.9108 6.0462 6.0462 0 0 0-6.5098-2.9A6.0651 6.0651 0 0 0 4.9807 4.1818a5.9847 5.9847 0 0 0-3.9977 2.9 6.0462 6.0462 0 0 0 .7427 7.0966 5.98 5.98 0 0 0 .511 4.9107 6.051 6.051 0 0 0 6.5146 2.9001A5.9847 5.9847 0 0 0 13.2599 24a6.0557 6.0557 0 0 0 5.7718-4.2058 5.9894 5.9894 0 0 0 3.9977-2.9001 6.0557 6.0557 0 0 0-.7475-7.0729zm-9.022 12.6081a4.4755 4.4755 0 0 1-2.8764-1.0408l.1419-.0804 4.7783-2.7582a.7948.7948 0 0 0 .3927-.6813v-6.7369l2.02 1.1686a.071.071 0 0 1 .038.052v5.5826a4.504 4.504 0 0 1-4.4945 4.4944zm-9.6607-4.1254a4.4708 4.4708 0 0 1-.5346-3.0137l.142.0852 4.783 2.7582a.7712.7712 0 0 0 .7806 0l5.8428-3.3685v2.3324a.0804.0804 0 0 1-.0332.0615L9.74 19.9502a4.4992 4.4992 0 0 1-6.1408-1.6464zM2.3408 7.8956a4.485 4.485 0 0 1 2.3655-1.9728V11.6a.7664.7664 0 0 0 .3879.6765l5.8144 3.3543-2.0201 1.1685a.0757.0757 0 0 1-.071 0l-4.8303-2.7865A4.504 4.504 0 0 1 2.3408 7.872zm16.5963 3.8558L13.1038 8.364 15.1192 7.2a.0757.0757 0 0 1 .071 0l4.8303 2.7913a4.4944 4.4944 0 0 1-.6765 8.1042v-5.6772a.79.79 0 0 0-.407-.667zm2.0107-3.0231l-.142-.0852-4.7735-2.7818a.7759.7759 0 0 0-.7854 0L9.409 9.2297V6.8974a.0662.0662 0 0 1 .0284-.0615l4.8303-2.7866a4.4992 4.4992 0 0 1 6.6802 4.66zM8.3065 12.863l-2.02-1.1638a.0804.0804 0 0 1-.038-.0567V6.0742a4.4992 4.4992 0 0 1 7.3757-3.4537l-.142.0805L8.704 5.459a.7948.7948 0 0 0-.3927.6813zm1.0976-2.3654l2.602-1.4998 2.6069 1.4998v2.9994l-2.5974 1.4997-2.6067-1.4997Z" />
    </svg>
  );
}

// ─── Small helpers ───────────────────────────────────────────────────────────

function ago(seconds: number): string {
  if (seconds < 5) return 'now';
  if (seconds < 60) return `${Math.floor(seconds)}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(n);
}

function statusColor(s: SessionRow): string {
  if (s.status === 'busy') return '#198754';
  if (s.live) return '#20c997';
  if (s.status === 'idle') return '#ffc107';
  return '#6c757d';
}

const ORIGIN_BADGE: Record<string, { label: string; color: string }> = {
  devserver: { label: 'agent', color: 'primary' },
  assistant: { label: 'ask agent', color: 'info' },
  console: { label: 'console', color: 'secondary' },
  external: { label: 'external', color: 'dark' },
};

// ─── Session list ────────────────────────────────────────────────────────────

function useSessionList(provider: SessionProvider) {
  // Rows are stored *tagged with the provider they came from* and handed out
  // only while that tag matches. Clearing them in an effect is not enough: on
  // the render where the tab flips, `provider` is already the new one while
  // state still holds the old provider's rows, and the transcript effect —
  // which re-runs on `provider` — would fire a tail request for the previous
  // provider's session id. That cross-provider request is the 404 the operator
  // saw on every tab switch. Gating during render closes the window.
  const [data, setData] = useState<{ provider: SessionProvider; rows: SessionRow[] }>(
    { provider, rows: [] },
  );
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    try {
      const res = await fetch(`/api/${provider}-sessions?limit=80`);
      const body = await res.json();
      if (!res.ok) {
        setError({ error: body?.error ?? `HTTP ${res.status}`, hint: body?.hint });
        setData({ provider, rows: [] });
      } else {
        setData({
          provider,
          rows: Array.isArray(body.sessions) ? body.sessions : [],
        });
        setError(
          body.home_exists === false
            ? {
                error: `${provider === 'claude' ? 'Claude' : 'Codex'} state directory not found`,
                hint: provider === 'claude'
                  ? `Looked in ${body.claude_home}. In Docker, bind-mount the host's ` +
                    '~/.claude read-only and set CLAUDE_HOME to the mount point.'
                  : `Looked in ${body.codex_home}. In Docker, bind-mount the host's ` +
                    '~/.codex read-only and set CODEX_HOME to the mount point.',
              }
            : null,
        );
      }
    } catch (err) {
      setError({
        error: err instanceof Error ? err.message : 'Request failed',
        hint: 'The Next.js server could not be reached.',
      });
    } finally {
      setLoading(false);
    }
  }, [provider]);

  useEffect(() => {
    setError(null);
    setLoading(true);
    load();
    const id = setInterval(load, LIST_POLL_MS);
    return () => clearInterval(id);
  }, [load]);

  // The render-time gate. Rows tagged with a different provider are simply
  // not visible, so nothing downstream can pair them with the current tab.
  const sessions = data.provider === provider ? data.rows : [];

  return { sessions, error, loading, reload: load };
}

// ─── Transcript tail ─────────────────────────────────────────────────────────

function useTranscript(sessionId: string | null, provider: SessionProvider) {
  const [events, setEvents] = useState<Ev[]>([]);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(false);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [truncatedHead, setTruncatedHead] = useState(false);

  const offsetRef = useRef(0);
  // Usage envelopes repeat across the lines of one assistant message; the
  // worker dedupes within a batch, this dedupes across polls.
  const seenUsageRef = useRef<Set<string>>(new Set());
  // One poll at a time: two overlapping polls read the same `since` offset and
  // would append the same events twice.
  const inFlightRef = useRef(false);
  // Bumped on every (re)start of the effect. A response from an older
  // generation — the previous session, or before a refresh — is dropped so it
  // can never write into the new session's offset/events.
  const genRef = useRef(0);
  const [refreshNonce, setRefreshNonce] = useState(0);

  useEffect(() => {
    const gen = ++genRef.current;
    const controller = new AbortController();
    const stale = () => gen !== genRef.current || controller.signal.aborted;

    inFlightRef.current = false;
    offsetRef.current = 0;
    seenUsageRef.current = new Set();
    setEvents([]);
    setTruncatedHead(false);
    setError(null);
    if (!sessionId) {
      setLoading(false);
      return () => controller.abort();
    }
    setLoading(true);

    const fetchTail = async () => {
      if (inFlightRef.current) return;
      inFlightRef.current = true;
      try {
        const res = await fetch(
          `/api/${provider}-sessions/${sessionId}/tail?since=${offsetRef.current}`,
          { signal: controller.signal },
        );
        const data = await res.json();
        if (stale()) return;
        if (!res.ok) {
          setError({ error: data?.error ?? `HTTP ${res.status}`, hint: data?.hint });
          return;
        }

        // The worker restarts us at 0 when the file shrank (rotated/restarted).
        if (data.reset) {
          seenUsageRef.current = new Set();
          setEvents([]);
        }
        offsetRef.current = data.next_offset ?? offsetRef.current;
        if (data.truncated_head) setTruncatedHead(true);

        const incoming: Ev[] = Array.isArray(data.events) ? data.events : [];
        const fresh = incoming.filter((e) => {
          if (e.type !== 'usage' || !e.msg_id) return true;
          if (seenUsageRef.current.has(e.msg_id)) return false;
          seenUsageRef.current.add(e.msg_id);
          return true;
        });

        setError(null);
        if (fresh.length > 0) {
          setEvents((prev) => (data.reset ? fresh : [...prev, ...fresh]));
          setLastUpdated(new Date());
        }
      } catch (err) {
        if (stale()) return; // aborted by a session switch / refresh / unmount
        setError({
          error: err instanceof Error ? err.message : 'Request failed',
          hint: 'The Next.js server could not be reached.',
        });
      } finally {
        // A stale request must not release the new generation's slot.
        if (!stale()) {
          inFlightRef.current = false;
          setLoading(false);
        }
      }
    };

    fetchTail();
    const id = setInterval(fetchTail, TAIL_POLL_MS);
    return () => {
      clearInterval(id);
      controller.abort();
    };
  }, [sessionId, provider, refreshNonce]);

  const refresh = useCallback(() => setRefreshNonce((n) => n + 1), []);

  return { events, error, loading, lastUpdated, truncatedHead, refresh };
}

// ─── Session list panel ──────────────────────────────────────────────────────

function SessionList({
  sessions,
  selected,
  onSelect,
  liveOnly,
  onLiveOnly,
  filter,
  onFilter,
}: {
  sessions: SessionRow[];
  selected: string | null;
  onSelect: (id: string) => void;
  liveOnly: boolean;
  onLiveOnly: (v: boolean) => void;
  filter: string;
  onFilter: (v: string) => void;
}) {
  return (
    <div className="d-flex flex-column h-100">
      <div className="d-flex align-items-center gap-2 mb-2">
        <CFormInput
          size="sm"
          placeholder="Filter…"
          value={filter}
          onChange={(e) => onFilter(e.target.value)}
        />
        <CFormCheck
          id="sessions-live-only"
          label="Live"
          className="flex-shrink-0 small"
          checked={liveOnly}
          onChange={(e) => onLiveOnly(e.target.checked)}
        />
      </div>

      <div className="flex-grow-1" style={{ overflowY: 'auto', minHeight: 0 }}>
        {sessions.length === 0 && (
          <div className="text-body-secondary small p-2">No sessions match.</div>
        )}
        {sessions.map((s) => {
          const isActive = s.session_id === selected;
          const origin = ORIGIN_BADGE[s.origin] ?? ORIGIN_BADGE.external;
          return (
            <button
              key={s.session_id}
              onClick={() => onSelect(s.session_id)}
              className={`w-100 text-start border-0 rounded mb-1 p-2${
                isActive ? ' bg-primary-subtle' : ' bg-transparent'
              }`}
              style={{ cursor: 'pointer' }}
            >
              <div className="d-flex align-items-center gap-2">
                <span
                  style={{
                    display: 'inline-block',
                    width: 8,
                    height: 8,
                    minWidth: 8,
                    borderRadius: '50%',
                    backgroundColor: statusColor(s),
                    animation: s.status === 'busy' ? 'session-pulse 2s ease-in-out infinite' : 'none',
                  }}
                />
                <span className="text-truncate fw-semibold" style={{ fontSize: '0.82rem' }}>
                  {s.label}
                </span>
                <span className="ms-auto text-body-secondary" style={{ fontSize: '0.7rem' }}>
                  {ago(s.idle_seconds)}
                </span>
              </div>

              <div className="d-flex align-items-center gap-1 flex-wrap mt-1">
                <CBadge color={origin.color} shape="rounded-pill" style={{ fontSize: '0.62rem' }}>
                  {origin.label}
                </CBadge>
                {s.task_key && (
                  <CBadge color="success" shape="rounded-pill" style={{ fontSize: '0.62rem' }}>
                    {s.task_key}
                  </CBadge>
                )}
                {s.repo_name && (
                  <CBadge color="light" className="text-dark" shape="rounded-pill" style={{ fontSize: '0.62rem' }}>
                    {s.repo_name}
                  </CBadge>
                )}
              </div>

              <div
                className="text-body-secondary text-truncate mt-1"
                style={{ fontSize: '0.68rem', direction: 'rtl', textAlign: 'left' }}
                title={s.cwd}
              >
                {s.cwd || '—'}
              </div>
            </button>
          );
        })}
      </div>
    </div>
  );
}

// ─── Transcript event rows ───────────────────────────────────────────────────

function ToolRow({ call, result }: { call: Ev; result: Ev | undefined }) {
  const [open, setOpen] = useState(false);
  const failed = result?.is_error;
  return (
    <div className="mb-1">
      <button
        onClick={() => setOpen((v) => !v)}
        className="w-100 text-start border-0 bg-transparent p-0 d-flex align-items-center gap-2"
        style={{ cursor: result ? 'pointer' : 'default', color: '#c8c8c8' }}
      >
        <span style={{ color: failed ? '#f48771' : '#9cdcfe', minWidth: 14 }}>
          {result ? (open ? '▾' : '▸') : '·'}
        </span>
        <span style={{ color: failed ? '#f48771' : '#569cd6', minWidth: 72 }}>{call.name}</span>
        <span className="text-truncate flex-grow-1" style={{ color: '#a0a0a0' }}>
          {call.summary}
        </span>
        {result && (
          <span style={{ color: failed ? '#f48771' : '#6a9955', whiteSpace: 'nowrap' }}>
            {failed ? 'error' : `${result.lines} ln`}
          </span>
        )}
      </button>
      {open && result && (
        <pre
          style={{
            margin: '4px 0 8px 26px',
            padding: '6px 10px',
            background: 'rgba(255,255,255,0.04)',
            borderRadius: 4,
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-all',
            color: failed ? '#f48771' : '#b5b5b5',
            maxHeight: 340,
            overflowY: 'auto',
          }}
        >
          {result.text}
          {result.truncated && <span style={{ color: '#dcdcaa' }}>{'\n… truncated'}</span>}
        </pre>
      )}
    </div>
  );
}

function ThinkingRow({ ev }: { ev: Ev }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="mb-2">
      <button
        onClick={() => setOpen((v) => !v)}
        className="border-0 bg-transparent p-0 d-flex align-items-center gap-2"
        style={{ cursor: 'pointer', color: '#7a7a7a', fontStyle: 'italic' }}
      >
        <span>{open ? '▾' : '▸'}</span>
        <span>thinking</span>
      </button>
      {open && (
        <div
          style={{
            margin: '4px 0 0 22px',
            color: '#8a8a8a',
            fontStyle: 'italic',
            whiteSpace: 'pre-wrap',
          }}
        >
          {ev.text}
        </div>
      )}
    </div>
  );
}

function MessageRow({ ev }: { ev: Ev }) {
  const isUser = ev.role === 'user';
  return (
    <div
      className="mb-3"
      style={{
        borderLeft: `2px solid ${isUser ? '#0d6efd' : '#495057'}`,
        paddingLeft: 10,
        opacity: ev.sidechain ? 0.7 : 1,
      }}
    >
      <div
        className="d-flex align-items-center gap-2 mb-1"
        style={{ fontSize: '0.68rem', color: '#7a7a7a', textTransform: 'uppercase' }}
      >
        <span style={{ color: isUser ? '#6ea8fe' : '#adb5bd' }}>{isUser ? 'user' : 'assistant'}</span>
        {ev.model && <span>{ev.model}</span>}
        {ev.sidechain && <span>subagent</span>}
        {ev.ts && <span>{new Date(ev.ts).toLocaleTimeString()}</span>}
        {ev.redacted && ev.redacted.length > 0 && (
          <CBadge color="warning" shape="rounded-pill" style={{ fontSize: '0.6rem' }}>
            redacted
          </CBadge>
        )}
      </div>
      <div style={{ color: '#d4d4d4' }} className="session-md">
        <AskAgentMarkdown text={ev.text ?? ''} />
      </div>
      {ev.truncated && <div style={{ color: '#dcdcaa' }}>… truncated</div>}
    </div>
  );
}

/**
 * Copy `text`, returning whether it landed on the clipboard.
 *
 * The async Clipboard API only exists in a secure context, and the dashboard is
 * routinely served as plain http over a LAN address — where Chrome leaves
 * `navigator.clipboard` undefined and Safari rejects the write. So the
 * `execCommand` path is the norm here, not an exotic fallback: a throwaway
 * offscreen textarea, selected and copied inside the same user gesture (Safari
 * refuses otherwise), then removed. Focus is restored so the click does not
 * steal it from whatever the operator was on.
 */
async function copyText(text: string): Promise<boolean> {
  if (typeof window === 'undefined') return false;

  if (window.isSecureContext && navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // permission denied or a non-focused document — fall through
    }
  }

  const active = document.activeElement as HTMLElement | null;
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.setAttribute('readonly', ''); // stops the iOS keyboard from popping up
  // Off-screen rather than display:none — a hidden element cannot be selected.
  ta.style.cssText = 'position:fixed;top:0;left:-9999px;opacity:0;';
  document.body.appendChild(ta);
  try {
    ta.select();
    ta.setSelectionRange(0, text.length); // Safari ignores select() alone
    return document.execCommand('copy');
  } catch {
    return false;
  } finally {
    document.body.removeChild(ta);
    active?.focus?.();
  }
}

// ─── Transcript panel ────────────────────────────────────────────────────────

function TranscriptPanel({
  session,
  provider,
  onDelete,
  deleting,
}: {
  session: SessionRow | null;
  provider: SessionProvider;
  onDelete: () => void;
  deleting: boolean;
}) {
  const { events, error, loading, lastUpdated, truncatedHead, refresh } =
    useTranscript(session?.session_id ?? null, provider);

  const containerRef = useRef<HTMLDivElement>(null);
  const [atBottom, setAtBottom] = useState(true);
  const [copied, setCopied] = useState(false);

  // Reset the copy confirmation when the operator switches sessions, or the
  // tick would claim the id on screen is the one on the clipboard.
  useEffect(() => setCopied(false), [session?.session_id]);

  const copySessionId = useCallback(async () => {
    if (!session) return;
    if (!(await copyText(session.session_id))) return; // leave the button untouched
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  }, [session]);

  useLayoutEffect(() => {
    const el = containerRef.current;
    if (el && atBottom) el.scrollTop = el.scrollHeight;
  }, [events, atBottom]);

  const handleScroll = useCallback(() => {
    const el = containerRef.current;
    if (!el) return;
    setAtBottom(el.scrollHeight - el.scrollTop - el.clientHeight < 40);
  }, []);

  const scrollToBottom = useCallback(() => {
    const el = containerRef.current;
    if (el) {
      el.scrollTop = el.scrollHeight;
      setAtBottom(true);
    }
  }, []);

  // Pair each tool_call with its result so they render as one collapsible row.
  const { rows, stats } = useMemo(() => {
    const resultsById = new Map<string, Ev>();
    for (const e of events) {
      if (e.type === 'tool_result' && e.tool_use_id) resultsById.set(e.tool_use_id, e);
    }
    const claimed = new Set<string>();
    const out: React.ReactNode[] = [];
    let turns = 0;
    let inTok = 0;
    let outTok = 0;
    let cacheTok = 0;

    events.forEach((e, i) => {
      switch (e.type) {
        case 'usage':
          turns += 1;
          inTok += e.input_tokens ?? 0;
          outTok += e.output_tokens ?? 0;
          cacheTok += (e.cache_read_tokens ?? 0) + (e.cache_write_tokens ?? 0);
          break;
        case 'text':
          out.push(<MessageRow key={i} ev={e} />);
          break;
        case 'thinking':
          out.push(<ThinkingRow key={i} ev={e} />);
          break;
        case 'tool_call': {
          const result = e.id ? resultsById.get(e.id) : undefined;
          if (result && e.id) claimed.add(e.id);
          out.push(<ToolRow key={i} call={e} result={result} />);
          break;
        }
        case 'tool_result':
          // Orphan result (its call scrolled out of the loaded window).
          if (!e.tool_use_id || !claimed.has(e.tool_use_id)) {
            out.push(
              <ToolRow key={i} call={{ type: 'tool_call', name: 'result', summary: '' }} result={e} />,
            );
          }
          break;
        case 'notice':
          out.push(
            <div key={i} style={{ color: '#6a9955', fontSize: '0.72rem' }} className="mb-1">
              {e.text}
            </div>,
          );
          break;
      }
    });

    return { rows: out, stats: { turns, inTok, outTok, cacheTok } };
  }, [events]);

  if (!session) {
    return (
      <div className="d-flex flex-column align-items-center justify-content-center h-100 text-body-secondary">
        <CIcon icon={cilTerminal} size="xl" className="mb-2 opacity-50" />
        <span className="small">Select a session to watch its transcript.</span>
      </div>
    );
  }

  return (
    <div className="d-flex flex-column h-100">
      {/* Header. Refresh leads the row and Clear ends it, as far apart as the
          bar allows: one is routine, the other throws the transcript away. */}
      <div className="d-flex align-items-center gap-2 mb-2 flex-wrap">
        <CButton size="sm" color="secondary" variant="ghost" onClick={refresh} title="Reload transcript">
          <CIcon icon={cilSync} />
        </CButton>
        <span
          style={{
            display: 'inline-block',
            width: 8,
            height: 8,
            borderRadius: '50%',
            backgroundColor: error ? '#dc3545' : statusColor(session),
            animation: session.status === 'busy' && !error
              ? 'session-pulse 2s ease-in-out infinite'
              : 'none',
          }}
        />
        <span className="fw-semibold">{session.label}</span>
        {session.task_key && (session.task_id ? (
          <Link href={`/tasks/${session.task_id}`} className="text-decoration-none">
            <CBadge color="success" shape="rounded-pill">{session.task_key}</CBadge>
          </Link>
        ) : (
          <CBadge color="success" shape="rounded-pill">{session.task_key}</CBadge>
        ))}
        {session.git_branch && (
          <CBadge color="light" className="text-dark" shape="rounded-pill">
            {session.git_branch}
          </CBadge>
        )}
        {session.version && (
          <span className="text-body-secondary small">v{session.version}</span>
        )}
        {session.pid && <span className="text-body-secondary small">pid {session.pid}</span>}

        <div className="ms-auto d-flex align-items-center gap-2">
          <span className="text-body-secondary small">
            {stats.turns} turn{stats.turns !== 1 ? 's' : ''} ·{' '}
            {formatTokens(stats.outTok)} out · {formatTokens(stats.cacheTok)} cached
          </span>
          <span className="text-body-secondary small">
            {lastUpdated
              ? lastUpdated.toLocaleTimeString([], {
                  hour: '2-digit', minute: '2-digit', second: '2-digit',
                })
              : '—'}
          </span>
          {/* The session id is what `--resume` and every log grep want, so it
              is on screen verbatim, selectable, next to the button that puts it
              on the clipboard. */}
          <code
            className="text-body-secondary small user-select-all"
            style={{ fontSize: '0.72rem' }}
            title={session.session_id}
          >
            {session.session_id}
          </code>
          <CButton
            size="sm"
            color={copied ? 'success' : 'secondary'}
            variant="ghost"
            onClick={copySessionId}
            title={`Copy session ID\n${session.session_id}`}
          >
            <CIcon icon={copied ? cilCheckAlt : cilCopy} />
          </CButton>
          {/* Destructive and irreversible — hence danger, a confirm in the
              handler, and a hard block while the session is still running. */}
          {provider === 'claude' && (
            <CButton
              size="sm"
              color="danger"
              variant="ghost"
              onClick={onDelete}
              disabled={deleting || session.live}
              title={
                session.live
                  ? 'Stop the session before deleting it'
                  : 'Delete this session from disk — permanent'
              }
            >
              {deleting ? <CSpinner size="sm" /> : <CIcon icon={cilTrash} />}
            </CButton>
          )}
        </div>
      </div>

      <div className="text-body-secondary text-truncate mb-2" style={{ fontSize: '0.72rem' }}>
        {session.cwd}
      </div>

      {error && (
        <div className="alert alert-danger py-2 px-3 mb-2">
          <div className="fw-semibold small">{error.error}</div>
          {error.hint && <div className="small">{error.hint}</div>}
        </div>
      )}

      {/* Transcript */}
      <div className="position-relative flex-grow-1" style={{ minHeight: 0 }}>
        {loading && events.length === 0 && (
          <div className="d-flex align-items-center justify-content-center h-100">
            <CSpinner size="sm" className="me-2" />
            <span className="text-body-secondary small">Loading transcript…</span>
          </div>
        )}
        {!loading && events.length === 0 && !error && (
          <div className="d-flex align-items-center justify-content-center h-100">
            <span className="text-body-secondary small">Transcript is empty.</span>
          </div>
        )}

        <div
          ref={containerRef}
          onScroll={handleScroll}
          style={{
            height: '100%',
            overflowY: 'auto',
            padding: '12px 16px',
            fontSize: '0.78rem',
            lineHeight: 1.55,
            fontFamily: 'ui-monospace, "SFMono-Regular", "Cascadia Code", Menlo, monospace',
            backgroundColor: 'var(--cui-dark, #1a1d23)',
            color: '#d4d4d4',
            borderRadius: 6,
            display: events.length === 0 ? 'none' : 'block',
          }}
        >
          {truncatedHead && (
            <div style={{ color: '#dcdcaa' }} className="mb-2">
              … earlier turns omitted (showing the tail of a large transcript)
            </div>
          )}
          {rows}
        </div>

        {!atBottom && events.length > 0 && (
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

// ─── Main view ───────────────────────────────────────────────────────────────

export function SessionsView() {
  const [provider, setProvider] = useState<SessionProvider>('claude');
  const { sessions, error, loading, reload } = useSessionList(provider);
  const [selected, setSelected] = useState<string | null>(null);
  const [liveOnly, setLiveOnly] = useState(false);
  const [filter, setFilter] = useState('');
  const [deleting, setDeleting] = useState(false);
  const [actionError, setActionError] = useState<ApiError | null>(null);

  useEffect(() => {
    setSelected(null);
    setActionError(null);
  }, [provider]);

  const visible = useMemo(() => {
    const q = filter.trim().toLowerCase();
    return sessions.filter((s) => {
      if (liveOnly && !s.live) return false;
      if (!q) return true;
      return [s.label, s.cwd, s.repo_name, s.task_key, s.git_branch, s.origin]
        .some((v) => (v ?? '').toLowerCase().includes(q));
    });
  }, [sessions, liveOnly, filter]);

  // Select the most recently active session on first load.
  useEffect(() => {
    if (selected === null && visible.length > 0) setSelected(visible[0].session_id);
  }, [visible, selected]);

  const current = sessions.find((s) => s.session_id === selected) ?? null;
  const liveCount = sessions.filter((s) => s.live).length;

  const handleDelete = useCallback(async () => {
    if (!current) return;
    // Deliberately spells out what goes and that it cannot be undone: the
    // button sits next to routine controls but this one destroys operator data.
    const ok = confirm(
      `Delete session "${current.label}" from disk?\n\n` +
        'This erases its transcript, session-env and file-history under ' +
        `${'~/.claude'}. It cannot be undone.`,
    );
    if (!ok) return;

    setDeleting(true);
    try {
      const res = await fetch(`/api/claude-sessions/${current.session_id}`, {
        method: 'DELETE',
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        setActionError({ error: data?.error ?? `HTTP ${res.status}`, hint: data?.hint });
        return;
      }
      const errs: string[] = Array.isArray(data?.errors) ? data.errors : [];
      setActionError(
        errs.length > 0
          ? { error: 'Session partly deleted', hint: `Could not remove: ${errs.join('; ')}` }
          : null,
      );
      // Drop the selection so the auto-select effect lands on the next
      // session once the reloaded list arrives.
      setSelected(null);
      await reload();
    } catch (err) {
      setActionError({
        error: err instanceof Error ? err.message : 'Delete failed',
        hint: 'Nothing was deleted.',
      });
    } finally {
      setDeleting(false);
    }
  }, [current, reload]);

  return (
    <>
      <div className="d-flex align-items-center gap-2 mb-2">
        <h5 className="mb-0">Sessions</h5>
        <CButtonGroup size="sm" role="group" aria-label="Session provider">
          <CButton
            color={provider === 'claude' ? 'primary' : 'secondary'}
            variant={provider === 'claude' ? undefined : 'outline'}
            onClick={() => setProvider('claude')}
            className="d-flex align-items-center gap-2"
            aria-pressed={provider === 'claude'}
          >
            <ClaudeIcon />
            Claude
          </CButton>
          <CButton
            color={provider === 'codex' ? 'primary' : 'secondary'}
            variant={provider === 'codex' ? undefined : 'outline'}
            onClick={() => setProvider('codex')}
            className="d-flex align-items-center gap-2"
            aria-pressed={provider === 'codex'}
          >
            <ChatGptIcon />
            Codex
          </CButton>
        </CButtonGroup>
        <span className="text-body-secondary small">
          {liveCount} live · {sessions.length} on this host
        </span>
      </div>

      {[actionError, error].filter(Boolean).map((e, i) => (
        <div key={i} className="alert alert-warning py-1 px-2 mb-2">
          <div className="fw-semibold small">{e!.error}</div>
          {e!.hint && <div className="small">{e!.hint}</div>}
        </div>
      ))}

      <CCard>
        <CCardBody className="p-2">
          {/* Sized against the shell's dense padding (AppShell DENSE_ROUTES):
              header 64 + footer 48 + container/card padding + this page's
              own heading row. */}
          <div
            className="d-flex gap-2"
            style={{ height: 'calc(100vh - 180px)', minHeight: 420 }}
          >
            <div
              className="border-end pe-2"
              style={{ width: 260, minWidth: 260 }}
            >
              {loading && sessions.length === 0 ? (
                <div className="d-flex align-items-center gap-2 p-2">
                  <CSpinner size="sm" />
                  <span className="text-body-secondary small">Scanning…</span>
                </div>
              ) : (
                <SessionList
                  sessions={visible}
                  selected={selected}
                  onSelect={setSelected}
                  liveOnly={liveOnly}
                  onLiveOnly={setLiveOnly}
                  filter={filter}
                  onFilter={setFilter}
                />
              )}
            </div>

            <div className="flex-grow-1" style={{ minWidth: 0 }}>
              <TranscriptPanel
                session={current}
                provider={provider}
                onDelete={handleDelete}
                deleting={deleting}
              />
            </div>
          </div>
        </CCardBody>
      </CCard>

      <style>{`
        @keyframes session-pulse {
          0%, 100% { opacity: 1; }
          50%      { opacity: 0.35; }
        }
        .session-md p:last-child { margin-bottom: 0; }
        .session-md pre {
          background: rgba(255,255,255,0.05);
          padding: 8px 10px;
          border-radius: 4px;
          overflow-x: auto;
        }
        .session-md code { color: #ce9178; }
      `}</style>
    </>
  );
}
