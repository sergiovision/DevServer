'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import {
  CBadge,
  CButton,
  CCard,
  CCardBody,
  CCardHeader,
  CFormTextarea,
  CSpinner,
} from '@coreui/react-pro';
import { EmptyState } from '@/components/EmptyState';
import { api, errorMessage } from '@/lib/api-client';

interface TaskMessage {
  id: number;
  from_task_id: number | null;
  from_task_key: string | null;
  to_task_id: number | null;
  to_task_key: string;
  kind: string;
  subject: string | null;
  body: string;
  payload: Record<string, unknown>;
  read_at: string | null;
  created_at: string;
}

interface MessagesPanelProps {
  taskId: number;
  taskKey: string;
}

const OPERATOR = 'operator';
const POLL_MS = 5000;

const KIND_COLOR: Record<string, string> = {
  request: 'warning',
  handoff: 'primary',
  response: 'success',
  note: 'secondary',
};

/**
 * Two-way chat between the operator and a task over the inter-task message bus.
 *
 * Shows the task's thread (messages it sent — to other tasks or to the
 * operator — and messages it received) and lets the operator reply. The agent
 * reads replies on its next inbox check. Polls every 5 s while the tab is
 * visible so agent messages appear without a reload.
 */
export function MessagesPanel({ taskId, taskKey }: MessagesPanelProps) {
  const [messages, setMessages] = useState<TaskMessage[]>([]);
  const [loading, setLoading] = useState(true);
  const [sending, setSending] = useState(false);
  const [draft, setDraft] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const listRef = useRef<HTMLDivElement>(null);
  const inFlight = useRef(false);

  const load = useCallback(async () => {
    if (inFlight.current) return; // a slow worker must not stack requests
    inFlight.current = true;
    try {
      const data = await api.get<{ messages?: unknown }>(`/api/tasks/${taskId}/messages?limit=100`);
      setMessages(Array.isArray(data?.messages) ? (data.messages as TaskMessage[]) : []);
    } catch (err) {
      console.error('load messages failed', err);
    } finally {
      inFlight.current = false;
      setLoading(false);
    }
  }, [taskId]);

  useEffect(() => {
    load();
    const tick = () => {
      if (!document.hidden) load();
    };
    const iv = setInterval(tick, POLL_MS);
    document.addEventListener('visibilitychange', tick);
    return () => {
      clearInterval(iv);
      document.removeEventListener('visibilitychange', tick);
    };
  }, [load]);

  useEffect(() => {
    if (listRef.current) listRef.current.scrollTop = listRef.current.scrollHeight;
  }, [messages.length]);

  const send = async () => {
    if (!draft.trim()) return;
    setSending(true);
    setError('');
    setNotice('');
    try {
      const data = await api.post<{ live?: boolean } | null>(
        `/api/tasks/${taskId}/messages/as-operator`,
        { body: draft, kind: 'request' },
      );
      setDraft('');
      setNotice(
        data?.live
          ? 'Delivered. The agent will read it on its next inbox check.'
          : 'Delivered to the inbox. The agent is not running, so it will read it when the task runs again.',
      );
      await load();
    } catch (err) {
      setError(errorMessage(err, 'Could not send the message'));
    } finally {
      setSending(false);
    }
  };

  return (
    <CCard className="mb-4">
      <CCardHeader className="d-flex justify-content-between align-items-center">
        <strong>Messages</strong>
        <small className="text-body-secondary">{messages.length}</small>
      </CCardHeader>
      <CCardBody className="p-0">
        <div
          ref={listRef}
          style={{
            maxHeight: 320,
            overflowY: 'auto',
            padding: '0.75rem',
            background: 'var(--cui-tertiary-bg)',
          }}
        >
          {loading ? (
            <div className="text-center py-3">
              <CSpinner size="sm" />
            </div>
          ) : messages.length === 0 ? (
            <EmptyState
              title="No messages yet"
              hint={<>Agents can message each other and you; replies you send here land in this task&apos;s inbox.</>}
            />
          ) : (
            messages.map((m) => {
              const mine = m.from_task_key === OPERATOR;
              return (
                <div
                  key={m.id}
                  className={`mb-2 d-flex ${mine ? 'justify-content-end' : 'justify-content-start'}`}
                >
                  <div
                    style={{
                      maxWidth: '85%',
                      padding: '0.5rem 0.75rem',
                      borderRadius: 8,
                      background: mine ? 'var(--cui-primary-bg-subtle)' : 'var(--cui-body-bg)',
                      border: '1px solid var(--cui-border-color)',
                    }}
                  >
                    <div className="small mb-1">
                      <CBadge color={KIND_COLOR[m.kind] || 'secondary'}>{m.kind}</CBadge>{' '}
                      <strong>{mine ? 'you' : m.from_task_key || '(unknown)'}</strong>{' '}
                      <span className="text-body-secondary">
                        → {m.to_task_key === OPERATOR ? 'you' : m.to_task_key}
                      </span>
                    </div>
                    {m.subject && <div className="small fw-semibold mb-1">{m.subject}</div>}
                    <div className="small mb-1" style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                      {m.body}
                    </div>
                    <div className="small text-body-secondary" suppressHydrationWarning>
                      {new Date(m.created_at).toLocaleString()}
                      {m.read_at && ' · read'}
                    </div>
                  </div>
                </div>
              );
            })
          )}
        </div>
        <div className="border-top p-2">
          <CFormTextarea
            rows={2}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
                e.preventDefault();
                send();
              }
            }}
            placeholder="Message the agent… (Cmd/Ctrl+Enter to send)"
            disabled={sending}
            className="mb-2"
          />
          <div className="d-flex align-items-center gap-2">
            <div className="small flex-grow-1">
              {error && <span className="text-danger">{error}</span>}
              {notice && <span className="text-success">{notice}</span>}
            </div>
            <CButton color="primary" size="sm" onClick={send} disabled={sending || !draft.trim()}>
              {sending ? 'Sending…' : `Send to ${taskKey}`}
            </CButton>
          </div>
        </div>
      </CCardBody>
    </CCard>
  );
}
