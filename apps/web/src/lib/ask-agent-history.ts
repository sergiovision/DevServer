/**
 * Ask Agent conversation history — kept in the browser only.
 *
 * Why it exists: the Ask Agent transcript used to live in component state, so
 * closing the drawer or reloading the page lost it. It must not move into
 * Postgres either: the assistant's flagship use is "the database is down", and
 * a chat that needs the database to remember itself is useless exactly then.
 * So the last conversations live in `localStorage`.
 *
 * Invariants:
 * - ONE versioned key (`HISTORY_STORAGE_KEY`) holding
 *   `{version: HISTORY_VERSION, conversations: [...]}`. A different version or
 *   unparseable JSON is discarded (read as empty, never thrown) and simply
 *   overwritten by the next save.
 * - At most `MAX_CONVERSATIONS`, most recently updated first; saving a 21st
 *   evicts the oldest.
 * - Secrets are masked BEFORE anything is written: the shared redaction rules
 *   (`a2a/redact.ts`, a port of the worker's `secret_rules.py`) plus a
 *   key-based mask for `key=value`, `key: value` and JSON params whose key looks
 *   like a token / secret / password / api key / authorization header.
 * - The worker session id is never stored. The stored shape is built field by
 *   field, so a session id on the live object cannot leak through; a reopened
 *   chat starts a fresh worker session and sends its transcript as `history`.
 * - A proposed action that was never decided is stored as `stale`: the worker
 *   keeps pending actions in memory, so after a reload it could not be approved
 *   anyway, and offering a dead Run button would be a lie.
 *
 * The core is pure (state in, state out); `createHistoryStore` is the thin
 * adapter over an injectable Storage-like object so tests need no DOM.
 */

import { redactSecrets } from './a2a/redact';

export const HISTORY_STORAGE_KEY = 'devserver.askAgent.history.v1';
export const HISTORY_VERSION = 1;
export const MAX_CONVERSATIONS = 20;
export const MASK = '[REDACTED]';
const TITLE_MAX_CHARS = 60;
const DEFAULT_TITLE = 'New conversation';

// ── Types ───────────────────────────────────────────────────────────────────

export type ActionRisk = 'low' | 'medium' | 'high';
export type ActionDecision = 'approve' | 'reject';

export interface ConversationToolActivity {
  id: string;
  name: string;
  ok?: boolean;
  preview?: string;
}

export interface ConversationAction {
  actionId: string;
  kind: string;
  summary: string;
  params: Record<string, unknown>;
  risk: ActionRisk;
  decided?: ActionDecision;
  /** Undecided when the conversation was stored — can no longer be approved. */
  stale?: boolean;
}

export interface ConversationMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  /** Resolved `@mention` context sent with this user turn (not displayed). */
  contextBlock?: string;
  tools: ConversationToolActivity[];
  notices: string[];
  actions: ConversationAction[];
  output?: string;
  error?: string;
  /** The operator pressed Stop while this answer was streaming. */
  stopped?: boolean;
  /** Panel-only message (e.g. /help) — never sent, stored or exported. */
  local?: boolean;
}

export interface StoredConversation {
  id: string;
  title: string;
  /** True once the operator renamed it; the title is then never re-derived. */
  titleIsCustom: boolean;
  createdAt: number;
  updatedAt: number;
  messages: ConversationMessage[];
}

export interface HistoryState {
  version: typeof HISTORY_VERSION;
  conversations: StoredConversation[];
}

/** What the panel hands over to be saved; extra fields are ignored. */
export interface ConversationSnapshot {
  id: string;
  messages: readonly ConversationMessage[];
}

/** The subset of `window.localStorage` the adapter needs. */
export interface StorageLike {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

// ── Masking ─────────────────────────────────────────────────────────────────

const SECRET_KEY_WORDS = new Set([
  'token',
  'secret',
  'password',
  'passwd',
  'pwd',
  'passphrase',
  'apikey',
  'authorization',
  'credential',
  'credentials',
  'cookie',
]);
const SECRET_KEY_PAIRS = new Set(['api_key', 'private_key', 'access_key', 'client_secret', 'auth_token']);

/**
 * Does this parameter name look like it holds a credential?
 *
 * Matches on whole name segments (snake, kebab, dot and camelCase all split),
 * so `GITEA_TOKEN`, `apiKey` and `x-api-key` are secret while `max_tokens`
 * or `author` are not.
 */
export function isSecretKey(key: string): boolean {
  const segments = key
    .replace(/([a-z0-9])([A-Z])/g, '$1_$2')
    .toLowerCase()
    .split(/[^a-z0-9]+/)
    .filter(Boolean);
  for (let i = 0; i < segments.length; i += 1) {
    if (SECRET_KEY_WORDS.has(segments[i])) return true;
    if (i + 1 < segments.length && SECRET_KEY_PAIRS.has(`${segments[i]}_${segments[i + 1]}`)) return true;
  }
  return false;
}

// key, separator (optionally closing a JSON/quoted key), optional auth scheme, value.
const KEY_VALUE_PATTERN =
  /([A-Za-z_][A-Za-z0-9_.-]*)(["']?[ \t]*[:=][ \t]*)((?:Bearer|Basic|Token)[ \t]+)?("[^"\n]*"|'[^'\n]*'|[^\s,;&"'`<>{}()[\]]+)/g;
const BEARER_PATTERN = /\b(Bearer|Basic)[ \t]+[A-Za-z0-9._~+/=-]{8,}/g;

/** Mask secrets inside free text: shared rules, then `key=value` pairs. */
export function maskSecretsInText(text: string): string {
  if (!text) return text;
  const redacted = redactSecrets(text).text;
  const keyed = redacted.replace(
    KEY_VALUE_PATTERN,
    (match: string, key: string, separator: string, scheme: string | undefined, value: string) => {
      if (!isSecretKey(key) || value.includes('[REDACTED')) return match;
      const quote = value.startsWith('"') || value.startsWith("'") ? value[0] : '';
      return `${key}${separator}${scheme ?? ''}${quote}${MASK}${quote}`;
    },
  );
  return keyed.replace(BEARER_PATTERN, (_match: string, scheme: string) => `${scheme} ${MASK}`);
}

/** Mask a structured params value: secret-named keys wholesale, strings by rule. */
export function maskParams(value: unknown, depth = 0): unknown {
  if (typeof value === 'string') return maskSecretsInText(value);
  if (value === null || typeof value !== 'object') return value;
  if (depth > 16) return null;
  if (Array.isArray(value)) return value.map((item) => maskParams(item, depth + 1));
  const masked: Record<string, unknown> = {};
  for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
    const holdsValue = item !== null && item !== '' && typeof item !== 'boolean';
    masked[key] = isSecretKey(key) && holdsValue ? MASK : maskParams(item, depth + 1);
  }
  return masked;
}

function maskOptional(text: string | undefined): string | undefined {
  return text === undefined ? undefined : maskSecretsInText(text);
}

// ── Pure core ───────────────────────────────────────────────────────────────

export function emptyHistory(): HistoryState {
  return { version: HISTORY_VERSION, conversations: [] };
}

/** A conversation's default title: its first question, collapsed and cut. */
export function deriveTitle(messages: readonly ConversationMessage[]): string {
  const first = messages.find((m) => m.role === 'user' && !m.local && m.content.trim());
  if (!first) return DEFAULT_TITLE;
  const flat = maskSecretsInText(first.content).replace(/\s+/g, ' ').trim();
  return flat.length > TITLE_MAX_CHARS ? `${flat.slice(0, TITLE_MAX_CHARS - 1)}…` : flat;
}

/**
 * The storable form of one message. Built field by field on purpose: anything
 * not named here (a session id, a streaming flag) cannot reach storage.
 */
export function toStoredMessage(message: ConversationMessage): ConversationMessage {
  const stored: ConversationMessage = {
    id: message.id,
    role: message.role,
    content: maskSecretsInText(message.content),
    tools: message.tools.map((tool) => ({
      id: tool.id,
      name: tool.name,
      ...(tool.ok === undefined ? {} : { ok: tool.ok }),
      ...(tool.preview === undefined ? {} : { preview: maskSecretsInText(tool.preview) }),
    })),
    notices: message.notices.map(maskSecretsInText),
    actions: message.actions.map((action) => ({
      actionId: action.actionId,
      kind: action.kind,
      summary: maskSecretsInText(action.summary),
      params: maskParams(action.params ?? {}) as Record<string, unknown>,
      risk: action.risk,
      ...(action.decided ? { decided: action.decided } : { stale: true }),
    })),
  };
  const contextBlock = maskOptional(message.contextBlock);
  const output = maskOptional(message.output);
  const error = maskOptional(message.error);
  if (contextBlock !== undefined) stored.contextBlock = contextBlock;
  if (output !== undefined) stored.output = output;
  if (error !== undefined) stored.error = error;
  if (message.stopped) stored.stopped = true;
  return stored;
}

function sortAndCap(conversations: StoredConversation[]): StoredConversation[] {
  return [...conversations].sort((a, b) => b.updatedAt - a.updatedAt).slice(0, MAX_CONVERSATIONS);
}

/**
 * Insert or replace a conversation, stamped `now`, moved to the front.
 * A snapshot with nothing storable (empty, or only panel-local messages) is a
 * no-op so a stray save never creates an empty history entry.
 */
export function upsertConversation(
  state: HistoryState,
  snapshot: ConversationSnapshot,
  now: number,
): HistoryState {
  const messages = snapshot.messages.filter((m) => !m.local).map(toStoredMessage);
  if (messages.length === 0) return state;
  const existing = state.conversations.find((c) => c.id === snapshot.id);
  const conversation: StoredConversation = {
    id: snapshot.id,
    title: existing?.titleIsCustom ? existing.title : deriveTitle(messages),
    titleIsCustom: existing?.titleIsCustom ?? false,
    createdAt: existing?.createdAt ?? now,
    updatedAt: now,
    messages,
  };
  const others = state.conversations.filter((c) => c.id !== snapshot.id);
  return { version: HISTORY_VERSION, conversations: sortAndCap([conversation, ...others]) };
}

/** Rename; a blank title reverts to the derived one. Does not reorder. */
export function renameConversation(state: HistoryState, id: string, title: string): HistoryState {
  const trimmed = maskSecretsInText(title.replace(/\s+/g, ' ').trim()).slice(0, 120);
  return {
    version: HISTORY_VERSION,
    conversations: state.conversations.map((c) =>
      c.id !== id
        ? c
        : trimmed
          ? { ...c, title: trimmed, titleIsCustom: true }
          : { ...c, title: deriveTitle(c.messages), titleIsCustom: false },
    ),
  };
}

export function deleteConversation(state: HistoryState, id: string): HistoryState {
  return { version: HISTORY_VERSION, conversations: state.conversations.filter((c) => c.id !== id) };
}

export function findConversation(state: HistoryState, id: string): StoredConversation | null {
  return state.conversations.find((c) => c.id === id) ?? null;
}

// ── (De)serialisation ───────────────────────────────────────────────────────

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function asString(value: unknown): string | undefined {
  return typeof value === 'string' ? value : undefined;
}

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === 'string') : [];
}

function normaliseMessage(raw: unknown): ConversationMessage | null {
  if (!isRecord(raw)) return null;
  const role = raw.role;
  if ((role !== 'user' && role !== 'assistant') || typeof raw.id !== 'string' || typeof raw.content !== 'string') {
    return null;
  }
  const tools: ConversationToolActivity[] = Array.isArray(raw.tools)
    ? raw.tools.filter(isRecord).flatMap((tool) =>
        typeof tool.id === 'string' && typeof tool.name === 'string'
          ? [
              {
                id: tool.id,
                name: tool.name,
                ...(typeof tool.ok === 'boolean' ? { ok: tool.ok } : {}),
                ...(typeof tool.preview === 'string' ? { preview: tool.preview } : {}),
              },
            ]
          : [],
      )
    : [];
  const actions: ConversationAction[] = Array.isArray(raw.actions)
    ? raw.actions.filter(isRecord).flatMap((action) => {
        if (typeof action.actionId !== 'string' || typeof action.kind !== 'string') return [];
        const risk: ActionRisk =
          action.risk === 'low' || action.risk === 'medium' || action.risk === 'high' ? action.risk : 'medium';
        const decided: ActionDecision | undefined =
          action.decided === 'approve' || action.decided === 'reject' ? action.decided : undefined;
        return [
          {
            actionId: action.actionId,
            kind: action.kind,
            summary: asString(action.summary) ?? '',
            params: isRecord(action.params) ? action.params : {},
            risk,
            // Anything undecided on disk is stale, whatever the stored flag says.
            ...(decided ? { decided } : { stale: true }),
          },
        ];
      })
    : [];
  const message: ConversationMessage = {
    id: raw.id,
    role,
    content: raw.content,
    tools,
    notices: strings(raw.notices),
    actions,
  };
  const contextBlock = asString(raw.contextBlock);
  const output = asString(raw.output);
  const error = asString(raw.error);
  if (contextBlock !== undefined) message.contextBlock = contextBlock;
  if (output !== undefined) message.output = output;
  if (error !== undefined) message.error = error;
  if (raw.stopped === true) message.stopped = true;
  return message;
}

function normaliseConversation(raw: unknown): StoredConversation | null {
  if (!isRecord(raw) || typeof raw.id !== 'string' || !Array.isArray(raw.messages)) return null;
  const messages = raw.messages.map(normaliseMessage).filter((m): m is ConversationMessage => m !== null);
  if (messages.length === 0) return null;
  const updatedAt = typeof raw.updatedAt === 'number' && Number.isFinite(raw.updatedAt) ? raw.updatedAt : 0;
  const createdAt = typeof raw.createdAt === 'number' && Number.isFinite(raw.createdAt) ? raw.createdAt : updatedAt;
  const titleIsCustom = raw.titleIsCustom === true && typeof raw.title === 'string' && raw.title.trim() !== '';
  return {
    id: raw.id,
    title: titleIsCustom ? (raw.title as string) : deriveTitle(messages),
    titleIsCustom,
    createdAt,
    updatedAt,
    messages,
  };
}

/**
 * Read a stored blob. Never throws: missing, corrupt or other-version data
 * reads as an empty history, and malformed individual entries are dropped.
 */
export function parseHistory(raw: string | null): HistoryState {
  if (!raw) return emptyHistory();
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return emptyHistory();
  }
  if (!isRecord(parsed) || parsed.version !== HISTORY_VERSION || !Array.isArray(parsed.conversations)) {
    return emptyHistory();
  }
  const conversations = parsed.conversations
    .map(normaliseConversation)
    .filter((c): c is StoredConversation => c !== null);
  return { version: HISTORY_VERSION, conversations: sortAndCap(conversations) };
}

export function serializeHistory(state: HistoryState): string {
  return JSON.stringify({ version: HISTORY_VERSION, conversations: sortAndCap(state.conversations) });
}

// ── Storage adapter ─────────────────────────────────────────────────────────

export interface HistoryStore {
  load(): HistoryState;
  /** Load the latest stored state, apply `change`, persist, return the result. */
  update(change: (state: HistoryState) => HistoryState): HistoryState;
}

/**
 * Wrap a Storage-like object. Re-reads before every write so two open tabs do
 * not silently erase each other's newest conversation. Storage failures
 * (private mode, quota) degrade to an in-memory result rather than throwing.
 */
export function createHistoryStore(storage: StorageLike | null): HistoryStore {
  const load = (): HistoryState => {
    if (!storage) return emptyHistory();
    try {
      return parseHistory(storage.getItem(HISTORY_STORAGE_KEY));
    } catch {
      return emptyHistory();
    }
  };
  return {
    load,
    update(change) {
      const next = change(load());
      if (storage) {
        try {
          storage.setItem(HISTORY_STORAGE_KEY, serializeHistory(next));
        } catch {
          /* storage unavailable — history still applies to this tab's view */
        }
      }
      return next;
    },
  };
}

/** The browser's localStorage, or null where it is unavailable (SSR, blocked). */
export function browserStorage(): StorageLike | null {
  try {
    return typeof window !== 'undefined' && window.localStorage ? window.localStorage : null;
  } catch {
    return null;
  }
}
