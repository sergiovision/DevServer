'use client';

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { CBadge, CButton, CFormTextarea, CSpinner } from '@coreui/react-pro';
import CIcon from '@coreui/icons-react';
import {
  cilX,
  cilPlus,
  cilCheckAlt,
  cilBan,
  cilChevronBottom,
  cilHistory,
  cilCloudDownload,
  cilActionUndo,
} from '@coreui/icons';
import { useAssistantContext } from './AssistantProvider';
import { AskAgentMarkdown } from './AskAgentMarkdown';
import { AskAgentHistory } from './AskAgentHistory';
import {
  askAgent,
  decideAction,
  fetchCapabilities,
  type AssistantCapabilities,
  type AssistantEvent,
  type ChatTurn,
} from '@/lib/assistant-client';
import { api } from '@/lib/api-client';
import {
  browserStorage,
  createHistoryStore,
  deleteConversation,
  deriveTitle,
  emptyHistory,
  findConversation,
  renameConversation,
  upsertConversation,
  type ConversationAction,
  type ConversationMessage,
  type ConversationToolActivity,
  type HistoryState,
} from '@/lib/ask-agent-history';
import {
  IDLE_RECALL,
  canEditAndResend,
  conversationToMarkdown,
  createListMentionResolver,
  decideComposerKey,
  editAndResend,
  exportFileName,
  filterSlashCommands,
  helpMarkdown,
  parseMentions,
  parseSlashCommand,
  recallNext,
  recallPrevious,
  resolveMentions,
  slashQuery,
  userPrompts,
  type PromptRecallState,
  type SlashCommandName,
} from '@/lib/ask-agent-composer';

/**
 * The Ask Agent drawer.
 *
 * Deliberately *not* a modal: no backdrop, so the operator can keep reading the
 * page they are asking about. Width is resizable by dragging the left edge and
 * remembered in localStorage, because a chat about code wants more room than a
 * chat about a button.
 *
 * The transcript is never persisted server-side: the assistant's flagship use
 * is "the database is down", and a chat that needs the database to remember
 * itself is useless exactly when it is needed. Recent conversations are kept
 * in this browser's localStorage instead (`lib/ask-agent-history.ts`: masked,
 * capped, no worker session id), and a reopened one resumes by sending its
 * transcript as `history` to a fresh worker session.
 *
 * Composer behaviour (keys, prompt recall, `/` commands, `@` mentions, edit and
 * resend, Markdown export) is decided in `lib/ask-agent-composer.ts`; this
 * component only performs the side effects.
 */

const WIDTH_KEY = 'devserver-ask-agent-width';
const MODEL_KEY = 'devserver-ask-agent-model';
const MIN_WIDTH = 340;
const MAX_WIDTH = 900;
const DEFAULT_WIDTH = 440;

type ToolActivity = ConversationToolActivity;
type ProposedAction = ConversationAction;

/** A transcript entry; `streaming` is live-only and never stored. */
interface Message extends ConversationMessage {
  streaming?: boolean;
}

// Ids must not collide with those of a conversation reopened from storage,
// so a per-load counter alone is not enough.
const idPrefix = Date.now().toString(36);
let messageSeq = 0;
const nextId = () => `m-${idPrefix}-${++messageSeq}`;

function newConversationId(): string {
  // randomUUID is missing outside secure contexts (plain http on a LAN IP).
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  return `c-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

function emptyMessage(role: Message['role'], content = ''): Message {
  return { id: nextId(), role, content, tools: [], notices: [], actions: [] };
}

/** What a turn sends upstream: resolved @mention context travels with its prompt. */
function toChatTurns(messages: readonly Message[]): ChatTurn[] {
  return messages
    .filter((m) => !m.local && m.content.trim() && !m.error)
    .map((m) => ({ role: m.role, content: m.contextBlock ? `${m.content}\n\n${m.contextBlock}` : m.content }));
}

const MENTION_TIMEOUT_MS = 5000;

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
  const { page, repoId, takePendingPrompt, pendingPromptVersion } = useAssistantContext();
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [caps, setCaps] = useState<AssistantCapabilities | null>(null);
  // Model for the next turn. Null means "whatever Settings → System LLM says";
  // the picker resolves that to caps.model once capabilities land.
  const [model, setModel] = useState<string | null>(null);
  const [width, setWidth] = useState(DEFAULT_WIDTH);
  const [conversationId, setConversationId] = useState<string>(newConversationId);
  const [history, setHistory] = useState<HistoryState>(emptyHistory);
  const [showHistory, setShowHistory] = useState(false);
  const [slashIndex, setSlashIndex] = useState(0);
  const [slashDismissed, setSlashDismissed] = useState(false);

  const listRef = useRef<HTMLDivElement | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const recallRef = useRef<PromptRecallState>(IDLE_RECALL);
  // Set by anything that changes the transcript; the save effect clears it.
  // Opening or starting a conversation must not bump it in the LRU order.
  const dirtyRef = useRef(false);
  const historyStore = useMemo(() => createHistoryStore(browserStorage()), []);

  useEffect(() => {
    setHistory(historyStore.load());
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
  }, [historyStore]);

  // The command palette's "Ask Agent: <query>" row hands its query over via
  // the provider; prefill (never auto-send) so the operator can still edit it.
  useEffect(() => {
    const prompt = takePendingPrompt();
    if (!prompt) return;
    setInput(prompt);
    textareaRef.current?.focus();
  }, [pendingPromptVersion, takePendingPrompt]);

  // Persist once a turn settles — never mid-stream, so a half-written answer
  // is not saved on every token.
  useEffect(() => {
    if (busy || !dirtyRef.current) return;
    dirtyRef.current = false;
    setHistory(
      historyStore.update((state) => upsertConversation(state, { id: conversationId, messages }, Date.now())),
    );
  }, [messages, busy, conversationId, historyStore]);

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
            return { ...m, content: m.content + event.text };
          case 'action_output':
            return { ...m, output: (m.output || '') + event.text };
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

  /** Mark the in-flight reply finished; `stopped` when the operator aborted it. */
  const settleReply = useCallback((replyId: string, stopped: boolean) => {
    setMessages((prev) =>
      prev.map((m) => (m.id === replyId ? { ...m, streaming: false, stopped: stopped || m.stopped } : m)),
    );
  }, []);

  /**
   * Leave the current conversation: stop any stream, save what is unsaved
   * (an interrupted answer is kept, marked stopped) and drop the worker
   * session so the next question starts fresh.
   */
  const leaveConversation = useCallback(
    (save: boolean) => {
      abortRef.current?.abort();
      if (save && (dirtyRef.current || busy)) {
        const settled = messages.map((m) => (m.streaming ? { ...m, streaming: false, stopped: true } : m));
        setHistory(
          historyStore.update((state) =>
            upsertConversation(state, { id: conversationId, messages: settled }, Date.now()),
          ),
        );
      }
      dirtyRef.current = false;
      recallRef.current = IDLE_RECALL;
      if (sessionId) {
        // Best-effort: drop the worker-side session so the next question starts a
        // genuinely fresh conversation instead of resuming this one. The
        // route always answers 200 — NotificationProvider patches window.fetch
        // and toasts any non-2xx, so a silent housekeeping call must never fail.
        fetch(`/api/assistant/sessions/${encodeURIComponent(sessionId)}/cancel`, {
          method: 'POST',
        }).catch(() => {});
      }
      setSessionId(null);
      setInput('');
    },
    [busy, messages, conversationId, sessionId, historyStore],
  );

  const newChat = useCallback(() => {
    leaveConversation(true);
    setMessages([]);
    setConversationId(newConversationId());
    setShowHistory(false);
    textareaRef.current?.focus();
  }, [leaveConversation]);

  const openConversation = useCallback(
    (id: string) => {
      if (id === conversationId) {
        setShowHistory(false);
        return;
      }
      const stored = findConversation(historyStore.load(), id);
      if (!stored) return;
      leaveConversation(true);
      // No session id is restored: the next turn opens a fresh worker session
      // and carries this transcript as `history`.
      setMessages(stored.messages.map((m) => ({ ...m })));
      setConversationId(stored.id);
      setShowHistory(false);
      textareaRef.current?.focus();
    },
    [conversationId, historyStore, leaveConversation],
  );

  const deleteStoredConversation = useCallback(
    (id: string) => {
      setHistory(historyStore.update((state) => deleteConversation(state, id)));
      if (id === conversationId) {
        leaveConversation(false);
        setMessages([]);
        setConversationId(newConversationId());
      }
    },
    [conversationId, historyStore, leaveConversation],
  );

  const renameStoredConversation = useCallback(
    (id: string, title: string) => {
      setHistory(historyStore.update((state) => renameConversation(state, id, title)));
    },
    [historyStore],
  );

  const exportConversation = useCallback(() => {
    if (!messages.some((m) => !m.local)) return;
    const title = findConversation(history, conversationId)?.title ?? deriveTitle(messages);
    const exportedAt = new Date();
    const blob = new Blob([conversationToMarkdown({ title, messages }, exportedAt)], {
      type: 'text/markdown;charset=utf-8',
    });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = exportFileName(title, exportedAt);
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  }, [messages, history, conversationId]);

  const runCommand = useCallback(
    (command: SlashCommandName) => {
      setInput('');
      setSlashDismissed(false);
      switch (command) {
        case 'new':
          newChat();
          return;
        case 'clear':
          // Deleting the active conversation also resets the panel to a fresh one.
          deleteStoredConversation(conversationId);
          return;
        case 'export':
          exportConversation();
          return;
        case 'history':
          setShowHistory((open) => !open);
          return;
        case 'help':
          setShowHistory(false);
          setMessages((prev) => [...prev, { ...emptyMessage('assistant', helpMarkdown()), local: true }]);
          return;
      }
    },
    [newChat, deleteStoredConversation, conversationId, exportConversation],
  );

  const send = useCallback(
    async (text: string) => {
      const question = text.trim();
      if (!question || busy) return;
      const command = parseSlashCommand(question);
      if (command) {
        runCommand(command);
        return;
      }

      const priorTurns = toChatTurns(messages);
      const userMsg = emptyMessage('user', question);
      const replyMsg: Message = { ...emptyMessage('assistant'), streaming: true };
      const replyId = replyMsg.id;
      setMessages((prev) => [...prev, userMsg, replyMsg]);
      setInput('');
      setShowHistory(false);
      recallRef.current = IDLE_RECALL;
      dirtyRef.current = true;
      setBusy(true);

      const controller = new AbortController();
      abortRef.current = controller;
      try {
        let outgoing = question;
        if (parseMentions(question).length > 0) {
          // Lookup failures (e.g. Postgres down) leave the raw @token in place.
          const resolver = createListMentionResolver((url) =>
            api.get<unknown>(url, { signal: AbortSignal.timeout(MENTION_TIMEOUT_MS) }),
          );
          const resolution = await resolveMentions(question, resolver);
          if (resolution.contextBlock) {
            const contextBlock = resolution.contextBlock;
            outgoing = resolution.message;
            setMessages((prev) => prev.map((m) => (m.id === userMsg.id ? { ...m, contextBlock } : m)));
          }
        }
        if (!controller.signal.aborted) {
          await askAgent(
            { message: outgoing, session_id: sessionId, repo_id: repoId, page, history: priorTurns, model },
            (event) => applyEvent(replyId, event),
            controller.signal,
          );
        }
      } finally {
        setBusy(false);
        if (abortRef.current === controller) abortRef.current = null;
        settleReply(replyId, controller.signal.aborted);
      }
    },
    [busy, messages, sessionId, repoId, page, applyEvent, model, runCommand, settleReply],
  );

  /** Approve or reject a proposal; the outcome streams into a new reply. */
  const onDecide = useCallback(
    async (msgId: string, action: ProposedAction, decision: 'approve' | 'reject') => {
      if (busy || action.stale) return;
      const replyId = nextId();
      setMessages((prev) => [
        ...prev.map((m) =>
          m.id === msgId
            ? {
                ...m,
                actions: m.actions.map((a) =>
                  a.actionId === action.actionId ? { ...a, decided: decision } : a,
                ),
              }
            : m,
        ),
        { ...emptyMessage('assistant'), id: replyId, streaming: true },
      ]);
      dirtyRef.current = true;
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
        if (abortRef.current === controller) abortRef.current = null;
        settleReply(replyId, controller.signal.aborted);
      }
    },
    [busy, applyEvent, settleReply],
  );

  const onEditAndResend = useCallback(() => {
    const result = editAndResend(messages);
    if (!result) return;
    dirtyRef.current = true;
    setMessages(result.messages);
    setInput(result.prompt);
    textareaRef.current?.focus();
  }, [messages]);

  const onModelChange = useCallback((next: string) => {
    setModel(next);
    try {
      window.localStorage.setItem(MODEL_KEY, next);
    } catch {
      /* storage unavailable — the choice still applies to this session */
    }
  }, []);

  const suggestions = useMemo(
    () => SUGGESTIONS_BY_ROUTE[page.route] ?? ['What can DevServer do?', 'How do I create a task?'],
    [page.route],
  );

  const query = slashQuery(input);
  const slashItems = query === null || slashDismissed ? [] : filterSlashCommands(query);
  const slashMenuOpen = slashItems.length > 0;
  const activeSlashIndex = slashMenuOpen ? Math.min(slashIndex, slashItems.length - 1) : 0;

  const onComposerKeyDown = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    const el = event.currentTarget;
    const action = decideComposerKey({
      key: event.key,
      shiftKey: event.shiftKey,
      isComposing: event.nativeEvent.isComposing,
      value: el.value,
      selectionStart: el.selectionStart,
      selectionEnd: el.selectionEnd,
      slashMenuOpen,
      recalling: recallRef.current.index !== null,
    });
    switch (action) {
      case 'send':
        event.preventDefault();
        void send(input);
        return;
      case 'recall-previous':
      case 'recall-next': {
        event.preventDefault();
        const step = action === 'recall-previous' ? recallPrevious : recallNext;
        const result = step(userPrompts(messages), recallRef.current, input);
        recallRef.current = result.state;
        setInput(result.value);
        return;
      }
      case 'menu-up':
        event.preventDefault();
        setSlashIndex((activeSlashIndex - 1 + slashItems.length) % slashItems.length);
        return;
      case 'menu-down':
        event.preventDefault();
        setSlashIndex((activeSlashIndex + 1) % slashItems.length);
        return;
      case 'menu-select':
        event.preventDefault();
        runCommand(slashItems[activeSlashIndex].name);
        return;
      case 'menu-close':
        // Close the menu only; the drawer's window-level Esc must not fire.
        event.preventDefault();
        event.stopPropagation();
        setSlashDismissed(true);
        return;
      default:
        return;
    }
  };

  const hasTranscript = messages.some((m) => !m.local);

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
            color={caps.workerUnreachable ? 'danger' : caps.tools ? 'success' : 'secondary'}
            shape="rounded-pill"
            className="fw-normal"
            title={
              caps.workerUnreachable
                ? 'The worker is unreachable'
                : `${caps.vendor ?? '?'} · ${caps.model ?? '?'}${caps.tools ? ' · tools enabled' : ''}`
            }
          >
            {caps.workerUnreachable ? 'offline' : caps.tools ? 'agent · tools' : 'chat'}
          </CBadge>
        )}

        {/* Model picker. Only the model — the vendor stays whatever
            Settings → System LLM is set to, since changing it also changes
            credentials and billing. */}
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
            active={showHistory}
            onClick={() => setShowHistory((open) => !open)}
            title="Recent conversations (/history)" aria-label="Recent conversations"
          >
            <CIcon icon={cilHistory} />
          </CButton>
          <CButton
            size="sm" color="secondary" variant="ghost"
            onClick={exportConversation} disabled={!hasTranscript}
            title="Export as Markdown (/export)" aria-label="Export as Markdown"
          >
            <CIcon icon={cilCloudDownload} />
          </CButton>
          <CButton
            size="sm" color="secondary" variant="ghost"
            onClick={newChat} title="New conversation (/new)" aria-label="New conversation"
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
        {showHistory ? (
          <AskAgentHistory
            conversations={history.conversations}
            activeId={conversationId}
            onOpen={openConversation}
            onRename={renameStoredConversation}
            onDelete={deleteStoredConversation}
            onNew={newChat}
          />
        ) : (
          <>
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
                <p className="small text-body-tertiary mt-3 mb-0">
                  Type <code>/</code> for commands, <code>@TASK-KEY</code>, <code>@repo:name</code> or{' '}
                  <code>@idea:id</code> to add context.
                </p>
              </div>
            )}

            {messages.map((m) => (
              <MessageBubble
                key={m.id}
                message={m}
                busy={busy}
                onDecide={(action, decision) => onDecide(m.id, action, decision)}
              />
            ))}
          </>
        )}
      </div>

      <footer className="border-top p-2 position-relative">
        {slashMenuOpen && (
          <div
            className="list-group position-absolute shadow-sm"
            style={{ bottom: '100%', left: 8, right: 8, zIndex: 10 }}
            role="listbox"
            aria-label="Commands"
          >
            {slashItems.map((command, index) => (
              <button
                key={command.name}
                type="button"
                role="option"
                aria-selected={index === activeSlashIndex}
                className={`list-group-item list-group-item-action py-1 small${index === activeSlashIndex ? ' active' : ''}`}
                // mousedown, not click: keep focus in the textarea.
                onMouseDown={(e) => {
                  e.preventDefault();
                  runCommand(command.name);
                }}
              >
                <code className={index === activeSlashIndex ? 'text-reset' : ''}>/{command.name}</code>
                <span className="ms-2">{command.description}</span>
              </button>
            ))}
          </div>
        )}
        <CFormTextarea
          ref={textareaRef}
          rows={2}
          value={input}
          placeholder="Ask about this page…  (Enter sends · Shift+Enter new line · / commands · @ mentions)"
          onChange={(e) => {
            recallRef.current = IDLE_RECALL;
            setSlashDismissed(false);
            setSlashIndex(0);
            setInput(e.target.value);
          }}
          onKeyDown={onComposerKeyDown}
          disabled={busy}
        />
        <div className="d-flex align-items-center gap-2 mt-2">
          <small className="text-body-tertiary text-truncate" title={page.path}>
            {page.entity?.key || page.entity?.name || page.path}
          </small>
          <div className="ms-auto d-flex gap-2">
            {!busy && canEditAndResend(messages) && (
              <CButton
                size="sm" color="secondary" variant="ghost"
                onClick={onEditAndResend}
                title="Put the stopped prompt back in the box and drop the stopped answer"
              >
                <CIcon icon={cilActionUndo} className="me-1" />Edit and resend
              </CButton>
            )}
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
  busy,
  onDecide,
}: {
  message: Message;
  busy: boolean;
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
          <ActionCard key={a.actionId} action={a} busy={busy} onDecide={onDecide} />
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
  busy,
  onDecide,
}: {
  action: ProposedAction;
  busy: boolean;
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
        <small className="text-body-secondary">
          {decided ? 'proposed' : action.stale ? 'expired' : 'needs your approval'}
        </small>
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
      ) : action.stale ? (
        // Pending actions live in the worker's memory; after a reload this one
        // can no longer be approved — ask again to get a fresh proposal.
        <small className="text-body-secondary">
          Expired — this proposal was not decided before the chat was saved. Ask again to re-propose it.
        </small>
      ) : (
        <div className="d-flex gap-2">
          <CButton
            size="sm" color="primary" disabled={busy}
            onClick={() => onDecide(action, 'approve')}
          >
            <CIcon icon={cilCheckAlt} className="me-1" />Run
          </CButton>
          <CButton
            size="sm" color="secondary" variant="outline" disabled={busy}
            onClick={() => onDecide(action, 'reject')}
          >
            <CIcon icon={cilBan} className="me-1" />Cancel
          </CButton>
        </div>
      )}
    </div>
  );
}
