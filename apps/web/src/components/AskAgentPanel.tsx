'use client';

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { CBadge, CButton, CFormTextarea, CSpinner } from '@coreui/react-pro';
import CIcon from '@coreui/icons-react';
import { cilX, cilPlus, cilCheckAlt, cilBan, cilChevronBottom } from '@coreui/icons';
import { useAssistantContext } from './AssistantProvider';
import { AskAgentMarkdown } from './AskAgentMarkdown';
import {
  askAgent,
  decideAction,
  fetchCapabilities,
  type AssistantCapabilities,
  type AssistantEvent,
  type ChatTurn,
} from '@/lib/assistant-client';

/**
 * The Ask Agent drawer.
 *
 * Deliberately *not* a modal: no backdrop, so the operator can keep reading the
 * page they are asking about. Width is resizable by dragging the left edge and
 * remembered in localStorage, because a chat about code wants more room than a
 * chat about a button.
 *
 * The transcript lives in component state only. It is intentionally not
 * persisted server-side: the assistant's flagship use is "the database is
 * down", and a chat that needs the database to remember itself is useless
 * exactly when it is needed.
 */

const WIDTH_KEY = 'devserver-ask-agent-width';
const MODEL_KEY = 'devserver-ask-agent-model';
const MIN_WIDTH = 340;
const MAX_WIDTH = 900;
const DEFAULT_WIDTH = 440;

interface ToolActivity {
  id: string;
  name: string;
  ok?: boolean;
  preview?: string;
}

interface ProposedAction {
  actionId: string;
  kind: string;
  summary: string;
  params: Record<string, unknown>;
  risk: 'low' | 'medium' | 'high';
  decided?: 'approve' | 'reject';
}

interface Message {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  tools: ToolActivity[];
  notices: string[];
  actions: ProposedAction[];
  output?: string;
  error?: string;
  streaming?: boolean;
}

let messageSeq = 0;
const nextId = () => `m${++messageSeq}`;

const SUGGESTIONS_BY_ROUTE: Record<string, string[]> = {
  '/': ['What am I looking at here?', 'Why is a task still queued?'],
  '/tasks': ['How do I create a task?', 'What does “blocked” mean?'],
  '/tasks/new': ['Which task type should I pick?', 'What do the git flows do?'],
  '/tasks/[id]': ['Why did this task fail?', 'What does Continue do?'],
  '/repos': ['How do I add a local repo?', 'What does a git flow change?'],
  '/repos/[id]': ['What do the build and test commands do?'],
  '/settings': ['What is the System LLM used for?', 'Which billing mode should I use?'],
  '/logs': ['Postgres is down — how do I restart it?'],
  '/templates': ['What is a template good for?'],
  '/ideas': ['How do ideas become tasks?'],
};

export function AskAgentPanel({ onClose }: { onClose: () => void }) {
  const { page, repoId } = useAssistantContext();
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [caps, setCaps] = useState<AssistantCapabilities | null>(null);
  // Model for the next turn. Null means "whatever Settings → System LLM says";
  // the picker resolves that to caps.model once capabilities land.
  const [model, setModel] = useState<string | null>(null);
  const [width, setWidth] = useState(DEFAULT_WIDTH);

  const listRef = useRef<HTMLDivElement | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

  useEffect(() => {
    const stored = Number(window.localStorage.getItem(WIDTH_KEY));
    if (stored >= MIN_WIDTH && stored <= MAX_WIDTH) setWidth(stored);
    fetchCapabilities().then((c) => {
      setCaps(c);
      // Keep a previous choice only while it is still one of this vendor's
      // models — the vendor can be changed in Settings under a stale tab.
      const stored = window.localStorage.getItem(MODEL_KEY);
      setModel(stored && c.models?.some((m) => m.id === stored) ? stored : null);
    });
    textareaRef.current?.focus();
    return () => abortRef.current?.abort();
  }, []);

  // Inset the page rather than sit on top of it. Without this the drawer covers
  // the header's right-hand controls (the theme toggle), which a real browser
  // pass caught immediately — the button was there but unclickable.
  useEffect(() => {
    document.body.classList.add('ask-agent-open');
    document.documentElement.style.setProperty('--ask-agent-width', `${width}px`);
    return () => {
      document.body.classList.remove('ask-agent-open');
      document.documentElement.style.removeProperty('--ask-agent-width');
    };
  }, [width]);

  // Follow the tail only while the operator is already near it, so scrolling
  // back to re-read an answer is not yanked away by the next token.
  useEffect(() => {
    const el = listRef.current;
    if (!el) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
    if (nearBottom) el.scrollTop = el.scrollHeight;
  }, [messages]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const startResize = useCallback((event: React.MouseEvent) => {
    event.preventDefault();
    const startX = event.clientX;
    const startWidth = Number(
      window.localStorage.getItem(WIDTH_KEY) || DEFAULT_WIDTH,
    );
    const onMove = (e: MouseEvent) => {
      const next = Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, startWidth + (startX - e.clientX)));
      setWidth(next);
    };
    const onUp = () => {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
      setWidth((w) => {
        window.localStorage.setItem(WIDTH_KEY, String(w));
        return w;
      });
    };
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
  }, []);

  /** Fold one stream event into the in-flight assistant message. */
  const applyEvent = useCallback((msgId: string, event: AssistantEvent) => {
    if (event.type === 'session') {
      setSessionId(event.session_id);
      return;
    }
    setMessages((prev) =>
      prev.map((m) => {
        if (m.id !== msgId) return m;
        switch (event.type) {
          case 'text_delta':
          case 'action_output':
            return event.type === 'text_delta'
              ? { ...m, content: m.content + event.text }
              : { ...m, output: (m.output || '') + event.text };
          case 'tool_call':
            return {
              ...m,
              tools: [...m.tools, { id: event.id, name: event.name }],
            };
          case 'tool_result':
            return {
              ...m,
              tools: m.tools.map((t) =>
                t.id === event.id ? { ...t, ok: event.ok, preview: event.preview } : t,
              ),
            };
          case 'action_proposed':
            return {
              ...m,
              actions: [
                ...m.actions,
                {
                  actionId: event.action_id,
                  kind: event.kind,
                  summary: event.summary,
                  params: event.params,
                  risk: event.risk,
                },
              ],
            };
          case 'notice':
            return { ...m, notices: [...m.notices, event.text] };
          case 'error':
            return { ...m, error: event.message, streaming: false };
          case 'done':
            // done.text is the authoritative full answer; deltas can be dropped
            // by a slow renderer but this never is.
            return {
              ...m,
              content: event.text?.trim() ? event.text : m.content,
              streaming: false,
            };
          default:
            return m;
        }
      }),
    );
  }, []);

  const send = useCallback(
    async (text: string) => {
      const question = text.trim();
      if (!question || busy) return;

      const history: ChatTurn[] = messages
        .filter((m) => m.content.trim() && !m.error)
        .map((m) => ({ role: m.role, content: m.content }));

      const userMsg: Message = {
        id: nextId(), role: 'user', content: question,
        tools: [], notices: [], actions: [],
      };
      const replyId = nextId();
      const replyMsg: Message = {
        id: replyId, role: 'assistant', content: '',
        tools: [], notices: [], actions: [], streaming: true,
      };
      setMessages((prev) => [...prev, userMsg, replyMsg]);
      setInput('');
      setBusy(true);

      const controller = new AbortController();
      abortRef.current = controller;
      try {
        await askAgent(
          { message: question, session_id: sessionId, repo_id: repoId, page, history, model },
          (event) => applyEvent(replyId, event),
          controller.signal,
        );
      } finally {
        setBusy(false);
        abortRef.current = null;
        setMessages((prev) =>
          prev.map((m) => (m.id === replyId ? { ...m, streaming: false } : m)),
        );
      }
    },
    [busy, messages, sessionId, repoId, page, applyEvent, model],
  );

  const onModelChange = useCallback((next: string) => {
    setModel(next);
    try {
      window.localStorage.setItem(MODEL_KEY, next);
    } catch {
      /* storage unavailable — the choice still applies to this session */
    }
  }, []);

  const onDecide = useCallback(
    async (msgId: string, action: ProposedAction, decision: 'approve' | 'reject') => {
      setMessages((prev) =>
        prev.map((m) =>
          m.id === msgId
            ? {
                ...m,
                actions: m.actions.map((a) =>
                  a.actionId === action.actionId ? { ...a, decided: decision } : a,
                ),
              }
            : m,
        ),
      );
      const replyId = nextId();
      setMessages((prev) => [
        ...prev,
        {
          id: replyId, role: 'assistant', content: '',
          tools: [], notices: [], actions: [], streaming: true,
        },
      ]);
      setBusy(true);
      const controller = new AbortController();
      abortRef.current = controller;
      try {
        await decideAction(
          action.actionId, decision,
          (event) => applyEvent(replyId, event),
          controller.signal,
        );
      } finally {
        setBusy(false);
        abortRef.current = null;
        setMessages((prev) =>
          prev.map((m) => (m.id === replyId ? { ...m, streaming: false } : m)),
        );
      }
    },
    [applyEvent],
  );

  const newChat = useCallback(() => {
    abortRef.current?.abort();
    if (sessionId) {
      // Best-effort: drop the worker-side session so the next question starts a
      // genuinely fresh agent conversation instead of resuming this one. The
      // route always answers 200 — NotificationProvider patches window.fetch
      // and toasts any non-2xx, so a silent housekeeping call must never fail.
      fetch(`/api/assistant/sessions/${encodeURIComponent(sessionId)}/cancel`, {
        method: 'POST',
      }).catch(() => {});
    }
    setMessages([]);
    setSessionId(null);
    setInput('');
    textareaRef.current?.focus();
  }, [sessionId]);

  const suggestions = useMemo(
    () => SUGGESTIONS_BY_ROUTE[page.route] ?? ['What can DevServer do?', 'How do I create a task?'],
    [page.route],
  );

  return (
    <aside
      className="ask-agent-panel d-flex flex-column"
      style={{ width }}
      aria-label="Ask Agent"
    >
      <div
        className="ask-agent-resizer"
        onMouseDown={startResize}
        role="separator"
        aria-orientation="vertical"
        title="Drag to resize"
      />

      <header className="d-flex align-items-center gap-2 px-3 py-2 border-bottom">
        <span className="fw-semibold">Ask Agent</span>
        {caps && (
          <CBadge
            color={caps.tools ? 'success' : 'secondary'}
            shape="rounded-pill"
            className="fw-normal"
            title={
              caps.workerUnreachable
                ? 'The worker is unreachable'
                : `${caps.vendor ?? '?'} · ${caps.model ?? '?'}${caps.tools ? ' · tools enabled' : ''}`
            }
          >
            {caps.workerUnreachable ? 'offline' : caps.tools ? 'pro · tools' : 'free'}
          </CBadge>
        )}

        {/* Model picker. Only the model — the vendor stays whatever
            Settings → System LLM is set to, since changing it also changes
            credentials, billing and whether the agent engine is eligible. */}
        {caps?.models && caps.models.length > 0 && (
          <select
            className="form-select form-select-sm"
            style={{ maxWidth: 190, minWidth: 0 }}
            value={model ?? caps.model ?? ''}
            onChange={(e) => onModelChange(e.target.value)}
            disabled={busy}
            title={`${caps.vendor ?? 'system LLM'} — model for the next turn`}
            aria-label="Model"
          >
            {caps.models.map((m) => (
              <option key={m.id} value={m.id}>
                {m.id === caps.model ? `${m.label} · default` : m.label}
              </option>
            ))}
          </select>
        )}

        <div className="ms-auto d-flex gap-1">
          <CButton
            size="sm" color="secondary" variant="ghost"
            onClick={newChat} title="New conversation" aria-label="New conversation"
          >
            <CIcon icon={cilPlus} />
          </CButton>
          <CButton
            size="sm" color="secondary" variant="ghost"
            onClick={onClose} title="Close (Esc)" aria-label="Close"
          >
            <CIcon icon={cilX} />
          </CButton>
        </div>
      </header>

      <div ref={listRef} className="flex-grow-1 px-3 py-3" style={{ overflowY: 'auto' }}>
        {messages.length === 0 && (
          <div className="text-body-secondary">
            <p className="mb-2">
              Ask about DevServer, or about the screen you are on right now.
            </p>
            <div className="d-flex flex-column align-items-start gap-1">
              {suggestions.map((s) => (
                <button
                  key={s}
                  type="button"
                  className="btn btn-sm btn-link p-0 text-start"
                  onClick={() => send(s)}
                >
                  {s}
                </button>
              ))}
            </div>
            {caps && !caps.tools && !caps.workerUnreachable && (
              <p className="mt-3 mb-0 small">
                Tool access — searching your repositories and proposing fixes — is a
                DevServer Pro feature.
              </p>
            )}
          </div>
        )}

        {messages.map((m) => (
          <MessageBubble
            key={m.id}
            message={m}
            onDecide={(action, decision) => onDecide(m.id, action, decision)}
          />
        ))}
      </div>

      <footer className="border-top p-2">
        <CFormTextarea
          ref={textareaRef}
          rows={2}
          value={input}
          placeholder="Ask about this page…  (⌘/Ctrl + Enter to send)"
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
              e.preventDefault();
              send(input);
            }
          }}
          disabled={busy}
        />
        <div className="d-flex align-items-center gap-2 mt-2">
          <small className="text-body-tertiary text-truncate" title={page.path}>
            {page.entity?.key || page.entity?.name || page.path}
          </small>
          <div className="ms-auto d-flex gap-2">
            {busy && (
              <CButton
                size="sm" color="secondary" variant="outline"
                onClick={() => abortRef.current?.abort()}
              >
                Stop
              </CButton>
            )}
            <CButton size="sm" color="primary" disabled={busy || !input.trim()}
                     onClick={() => send(input)}>
              {busy ? <CSpinner size="sm" /> : 'Send'}
            </CButton>
          </div>
        </div>
      </footer>
    </aside>
  );
}

function MessageBubble({
  message,
  onDecide,
}: {
  message: Message;
  onDecide: (action: ProposedAction, decision: 'approve' | 'reject') => void;
}) {
  const mine = message.role === 'user';
  return (
    <div className={`d-flex mb-3 ${mine ? 'justify-content-end' : ''}`}>
      <div
        className="rounded px-3 py-2"
        style={{
          maxWidth: mine ? '85%' : '100%',
          width: mine ? undefined : '100%',
          background: mine ? 'var(--cui-primary-bg-subtle)' : 'var(--cui-tertiary-bg)',
          // Explicit: the subtle-background tokens do not carry a paired
          // foreground, so an inherited colour goes low-contrast in dark mode.
          color: 'var(--cui-body-color)',
          border: '1px solid var(--cui-border-color)',
          fontSize: '0.9rem',
        }}
      >
        {message.notices.map((n, i) => (
          <div key={i} className="small text-body-secondary fst-italic mb-2">{n}</div>
        ))}

        {message.tools.length > 0 && <ToolTimeline tools={message.tools} />}

        {message.content ? (
          <AskAgentMarkdown text={message.content} />
        ) : message.streaming && message.tools.length === 0 ? (
          <span className="text-body-tertiary">
            <CSpinner size="sm" className="me-2" />thinking…
          </span>
        ) : null}

        {message.output && (
          <pre
            className="mt-2 mb-0 p-2 rounded"
            style={{
              background: 'var(--cui-body-bg)',
              border: '1px solid var(--cui-border-color)',
              maxHeight: 260, overflow: 'auto', fontSize: '0.78rem',
              whiteSpace: 'pre-wrap', wordBreak: 'break-word',
            }}
          >
            {message.output}
          </pre>
        )}

        {message.actions.map((a) => (
          <ActionCard key={a.actionId} action={a} onDecide={onDecide} />
        ))}

        {message.error && (
          <div className="small text-danger mt-1">{message.error}</div>
        )}
      </div>
    </div>
  );
}

function ToolTimeline({ tools }: { tools: ToolActivity[] }) {
  const [open, setOpen] = useState(false);
  const pending = tools.some((t) => t.ok === undefined);
  return (
    <div className="mb-2">
      <button
        type="button"
        className="btn btn-sm btn-link p-0 text-body-secondary text-decoration-none"
        onClick={() => setOpen((v) => !v)}
      >
        <CIcon
          icon={cilChevronBottom}
          size="sm"
          className="me-1"
          style={{ transform: open ? 'none' : 'rotate(-90deg)', transition: 'transform .15s' }}
        />
        <small>
          {pending && <CSpinner size="sm" className="me-1" />}
          {tools.length} {tools.length === 1 ? 'lookup' : 'lookups'}
        </small>
      </button>
      {open && (
        <ul className="list-unstyled small mb-0 mt-1 ps-3">
          {tools.map((t, i) => (
            <li key={`${t.id}-${i}`} className="text-body-secondary">
              <code style={{ fontSize: '0.78rem' }}>{prettyToolName(t.name)}</code>
              {t.ok === false && <span className="text-danger ms-1">failed</span>}
              {t.preview && (
                <div className="text-body-tertiary text-truncate" title={t.preview}>
                  {t.preview.slice(0, 160)}
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function prettyToolName(name: string): string {
  return name.replace(/^mcp__DevServer__/, '');
}

const RISK_COLOR: Record<string, string> = {
  low: 'info',
  medium: 'warning',
  high: 'danger',
};

function ActionCard({
  action,
  onDecide,
}: {
  action: ProposedAction;
  onDecide: (action: ProposedAction, decision: 'approve' | 'reject') => void;
}) {
  const decided = action.decided;
  return (
    <div
      className="mt-2 rounded p-2"
      style={{
        border: '1px solid var(--cui-border-color)',
        background: 'var(--cui-body-bg)',
      }}
    >
      <div className="d-flex align-items-center gap-2 mb-1">
        <CBadge color={RISK_COLOR[action.risk] || 'secondary'} shape="rounded-pill">
          {action.kind.replace(/_/g, ' ')}
        </CBadge>
        <small className="text-body-secondary">needs your approval</small>
      </div>
      <div className="small mb-2">{action.summary}</div>
      {Object.keys(action.params || {}).length > 0 && (
        <pre
          className="small mb-2 p-1 rounded"
          style={{
            background: 'var(--cui-tertiary-bg)',
            fontSize: '0.75rem',
            overflowX: 'auto',
          }}
        >
          {JSON.stringify(action.params, null, 2)}
        </pre>
      )}
      {decided ? (
        <small className="text-body-secondary">
          {decided === 'approve' ? 'Approved — running…' : 'Rejected.'}
        </small>
      ) : (
        <div className="d-flex gap-2">
          <CButton size="sm" color="primary" onClick={() => onDecide(action, 'approve')}>
            <CIcon icon={cilCheckAlt} className="me-1" />Run
          </CButton>
          <CButton
            size="sm" color="secondary" variant="outline"
            onClick={() => onDecide(action, 'reject')}
          >
            <CIcon icon={cilBan} className="me-1" />Cancel
          </CButton>
        </div>
      )}
    </div>
  );
}
