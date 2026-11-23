/**
 * Ask Agent composer logic — pure, DOM-free decisions behind the input box.
 *
 * Why it exists: keyboard handling, prompt recall, the `/` command menu,
 * `@` mentions, "edit and resend" and Markdown export are each small, but
 * tangled into a 600-line React component they were untestable and drifted.
 * The panel now asks this module *what* to do and only performs the side
 * effect (focus, fetch, download).
 *
 * Invariants:
 * - Enter sends, Shift+Enter inserts a newline, an IME composition is never
 *   interrupted, and ↑/↓ only recall prompts when the caret is at the edge —
 *   so multi-line editing keeps normal arrow-key behaviour.
 * - Mention resolution never fails the turn: an unresolvable or erroring
 *   mention leaves the raw `@token` in the question and adds no context
 *   (the panel must stay usable when Postgres is down).
 * - Exported Markdown passes through the same secret masking as storage.
 */

import { maskParams, maskSecretsInText, type ConversationMessage } from './ask-agent-history';

// ── Keyboard decisions ──────────────────────────────────────────────────────

export interface ComposerKeyInput {
  key: string;
  shiftKey: boolean;
  isComposing: boolean;
  value: string;
  selectionStart: number;
  selectionEnd: number;
  /** The `/` command menu is showing at least one command. */
  slashMenuOpen: boolean;
  /** The operator is currently stepping through recalled prompts. */
  recalling: boolean;
}

export type ComposerKeyAction =
  | 'send'
  | 'newline'
  | 'recall-previous'
  | 'recall-next'
  | 'menu-up'
  | 'menu-down'
  | 'menu-select'
  | 'menu-close'
  | 'default';

/** Decide what a keydown in the composer means. `default` = let the browser act. */
export function decideComposerKey(input: ComposerKeyInput): ComposerKeyAction {
  if (input.isComposing) return 'default';
  const collapsed = input.selectionStart === input.selectionEnd;

  if (input.slashMenuOpen) {
    if (input.key === 'ArrowUp') return 'menu-up';
    if (input.key === 'ArrowDown') return 'menu-down';
    if (input.key === 'Escape') return 'menu-close';
    if ((input.key === 'Enter' && !input.shiftKey) || input.key === 'Tab') return 'menu-select';
  }

  if (input.key === 'Enter') return input.shiftKey ? 'newline' : 'send';
  if (input.key === 'ArrowUp' && !input.shiftKey) {
    if (input.value === '' || (collapsed && input.selectionStart === 0)) return 'recall-previous';
    // While stepping through recalled prompts the caret sits at the end, so
    // keep walking back from anywhere on the first line.
    const onFirstLine = !input.value.slice(0, input.selectionStart).includes('\n');
    if (input.recalling && collapsed && onFirstLine) return 'recall-previous';
    return 'default';
  }
  if (input.key === 'ArrowDown' && !input.shiftKey) {
    const onLastLine = !input.value.slice(input.selectionEnd).includes('\n');
    if (input.recalling && collapsed && onLastLine) return 'recall-next';
    return 'default';
  }
  return 'default';
}

// ── Prompt recall (↑ / ↓) ───────────────────────────────────────────────────

export interface PromptRecallState {
  /** Index into the prompt list being shown; null when not recalling. */
  index: number | null;
  /** What the operator had typed before the first ↑, restored by the last ↓. */
  draft: string;
}

export const IDLE_RECALL: PromptRecallState = { index: null, draft: '' };

export interface RecallResult {
  state: PromptRecallState;
  value: string;
}

/** Earlier user prompts of a transcript, oldest first, for recall. */
export function userPrompts(messages: readonly Pick<ConversationMessage, 'role' | 'content' | 'local'>[]): string[] {
  return messages.filter((m) => m.role === 'user' && !m.local && m.content.trim()).map((m) => m.content);
}

export function recallPrevious(
  prompts: readonly string[],
  state: PromptRecallState,
  currentValue: string,
): RecallResult {
  if (prompts.length === 0) return { state, value: currentValue };
  if (state.index === null) {
    const index = prompts.length - 1;
    return { state: { index, draft: currentValue }, value: prompts[index] };
  }
  const index = Math.max(0, state.index - 1);
  return { state: { ...state, index }, value: prompts[index] };
}

export function recallNext(prompts: readonly string[], state: PromptRecallState, currentValue: string): RecallResult {
  if (state.index === null) return { state, value: currentValue };
  if (state.index < prompts.length - 1) {
    const index = state.index + 1;
    return { state: { ...state, index }, value: prompts[index] };
  }
  return { state: IDLE_RECALL, value: state.draft };
}

// ── Slash commands ──────────────────────────────────────────────────────────

export type SlashCommandName = 'new' | 'clear' | 'export' | 'history' | 'help';

export interface SlashCommand {
  name: SlashCommandName;
  description: string;
}

export const SLASH_COMMANDS: readonly SlashCommand[] = [
  { name: 'new', description: 'Start a new conversation (this one stays in history)' },
  { name: 'clear', description: 'Discard this conversation and remove it from history' },
  { name: 'export', description: 'Download this conversation as Markdown' },
  { name: 'history', description: 'Show or hide recent conversations' },
  { name: 'help', description: 'Show commands, mentions and keyboard shortcuts' },
];

/**
 * The menu filter while the operator is still typing a command word:
 * `"/"` → `""`, `"/ex"` → `"ex"`. Null once there is a space or newline (the
 * input is a question that merely starts with a slash) or no leading slash.
 */
export function slashQuery(value: string): string | null {
  const match = /^\/([A-Za-z]*)$/.exec(value);
  return match ? match[1].toLowerCase() : null;
}

/** Commands matching a query: prefix matches first, then substring matches. */
export function filterSlashCommands(query: string): SlashCommand[] {
  const q = query.toLowerCase();
  const prefix = SLASH_COMMANDS.filter((c) => c.name.startsWith(q));
  const inner = SLASH_COMMANDS.filter((c) => !c.name.startsWith(q) && c.name.includes(q));
  return [...prefix, ...inner];
}

/** An exact command typed in full (`"/new"`, `" /Export "`), else null. */
export function parseSlashCommand(value: string): SlashCommandName | null {
  const match = /^\/([A-Za-z]+)$/.exec(value.trim());
  if (!match) return null;
  const name = match[1].toLowerCase();
  return SLASH_COMMANDS.some((c) => c.name === name) ? (name as SlashCommandName) : null;
}

/** Body of the panel-local `/help` message. */
export function helpMarkdown(): string {
  const commands = SLASH_COMMANDS.map((c) => `- \`/${c.name}\` — ${c.description}`).join('\n');
  return [
    '**Commands**',
    commands,
    '',
    '**Mentions** — add context to a question:',
    '- `@TASK-KEY` — a task (e.g. `@DS-42`)',
    '- `@repo:name` — a repository',
    '- `@idea:123` — an idea',
    '',
    '**Keys** — Enter sends · Shift+Enter new line · ↑/↓ recall earlier prompts · Esc closes',
  ].join('\n');
}

// ── Mentions ────────────────────────────────────────────────────────────────

export type MentionKind = 'task' | 'repo' | 'idea';

export interface Mention {
  kind: MentionKind;
  /** Task key, repo name or idea id as typed. */
  ref: string;
  /** The token exactly as it appears in the question, e.g. `@repo:web`. */
  raw: string;
}

/** Returns a context summary for the mention, or null when it does not exist. */
export type MentionResolver = (mention: Mention) => Promise<string | null>;

// Must start the input or follow whitespace/an opening bracket, so e-mail
// addresses (`a@b.com`) are never taken for mentions.
const MENTION_PATTERN = /(^|[\s([{])@(repo:[A-Za-z0-9._/-]+|idea:\d+|[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)+)/g;

/** Every distinct `@` mention in the text, in order of appearance. */
export function parseMentions(text: string): Mention[] {
  const seen = new Set<string>();
  const mentions: Mention[] = [];
  for (const match of text.matchAll(MENTION_PATTERN)) {
    // Sentence punctuation is not part of a repo name: "see @repo:web." → "web".
    const body = match[2].replace(/[._/-]+$/, '');
    let mention: Mention;
    if (body.startsWith('repo:')) {
      const ref = body.slice(5);
      if (!ref) continue;
      mention = { kind: 'repo', ref, raw: `@${body}` };
    } else if (body.startsWith('idea:')) {
      mention = { kind: 'idea', ref: body.slice(5), raw: `@${body}` };
    } else {
      mention = { kind: 'task', ref: body.toUpperCase(), raw: `@${body}` };
    }
    const identity = `${mention.kind}:${mention.ref.toLowerCase()}`;
    if (seen.has(identity)) continue;
    seen.add(identity);
    mentions.push(mention);
  }
  return mentions;
}

export interface MentionResolution {
  /** Question plus the context block when anything resolved; else unchanged. */
  message: string;
  /** The appended block alone (null when nothing resolved). */
  contextBlock: string | null;
  resolved: Mention[];
  unresolved: Mention[];
}

const CONTEXT_ITEM_MAX_CHARS = 2000;

/**
 * Resolve every mention through `resolver` and append one context block.
 * A resolver that throws or answers null leaves that mention unresolved —
 * the raw `@token` stays in the question and the turn still goes out.
 */
export async function resolveMentions(question: string, resolver: MentionResolver): Promise<MentionResolution> {
  const mentions = parseMentions(question);
  const settled = await Promise.all(
    mentions.map(async (mention) => {
      try {
        return { mention, summary: await resolver(mention) };
      } catch {
        return { mention, summary: null };
      }
    }),
  );
  const resolved = settled.filter((s) => s.summary && s.summary.trim());
  const unresolved = settled.filter((s) => !(s.summary && s.summary.trim())).map((s) => s.mention);
  if (resolved.length === 0) {
    return { message: question, contextBlock: null, resolved: [], unresolved };
  }
  const sections = resolved.map(({ mention, summary }) => {
    const text = (summary ?? '').trim();
    const clipped = text.length > CONTEXT_ITEM_MAX_CHARS ? `${text.slice(0, CONTEXT_ITEM_MAX_CHARS)}…` : text;
    return `### ${mention.raw} (${mention.kind})\n${clipped}`;
  });
  const contextBlock = `---\nReferenced context:\n\n${sections.join('\n\n')}`;
  return {
    message: `${question}\n\n${contextBlock}`,
    contextBlock,
    resolved: resolved.map((s) => s.mention),
    unresolved,
  };
}

type Row = Record<string, unknown>;

function field(row: Row, key: string): string {
  const value = row[key];
  if (value === null || value === undefined) return '';
  return typeof value === 'string' ? value : String(value);
}

function summarise(lines: [string, string][], body: string): string {
  const header = lines.filter(([, value]) => value.trim()).map(([label, value]) => `${label}: ${value}`);
  return [...header, ...(body.trim() ? ['', body.trim()] : [])].join('\n');
}

/** Find a task row by key, case-insensitively. */
export function findTaskByKey(rows: readonly Row[], key: string): Row | null {
  const wanted = key.toLowerCase();
  return rows.find((row) => field(row, 'task_key').toLowerCase() === wanted) ?? null;
}

/** Find a repo row by name, case-insensitively. */
export function findRepoByName(rows: readonly Row[], name: string): Row | null {
  const wanted = name.toLowerCase();
  return rows.find((row) => field(row, 'name').toLowerCase() === wanted) ?? null;
}

export function findIdeaById(rows: readonly Row[], id: string): Row | null {
  return rows.find((row) => field(row, 'id') === id) ?? null;
}

export function summariseTask(row: Row): string {
  return summarise(
    [
      ['Task', `${field(row, 'task_key')} — ${field(row, 'title')}`],
      ['Status', field(row, 'status')],
      ['Type', field(row, 'task_type')],
      ['Priority', field(row, 'priority')],
      ['Repo', field(row, 'repo_name')],
      ['Error', field(row, 'error_message')],
    ],
    [field(row, 'description'), field(row, 'acceptance') && `Acceptance: ${field(row, 'acceptance')}`]
      .filter(Boolean)
      .join('\n\n'),
  );
}

export function summariseRepo(row: Row): string {
  // Credentials never leave through a mention: only descriptive fields are read.
  return summarise(
    [
      ['Repo', field(row, 'name')],
      ['Provider', field(row, 'provider')],
      ['Default branch', field(row, 'default_branch')],
      ['Build', field(row, 'build_cmd')],
      ['Test', field(row, 'test_cmd')],
      ['Lint', field(row, 'lint_cmd')],
      ['Active', field(row, 'active')],
    ],
    '',
  );
}

export function summariseIdea(row: Row): string {
  return summarise(
    [
      ['Idea', `#${field(row, 'id')} — ${field(row, 'title')}`],
      ['Kind', field(row, 'kind')],
      ['Node', field(row, 'node_type')],
      ['Status', field(row, 'node_status')],
    ],
    field(row, 'content'),
  );
}

/** Fetches decoded JSON for a same-origin `/api/*` URL; throws on failure. */
export type JsonGetter = (url: string) => Promise<unknown>;

export const MENTION_SOURCES: Readonly<Record<MentionKind, string>> = {
  // The list route has no key filter; ordered newest-first, so a generous
  // limit covers every task an operator would plausibly mention.
  task: '/api/tasks?limit=1000',
  repo: '/api/repos',
  idea: '/api/ideas',
};

/**
 * A resolver over the existing list endpoints. Each list is fetched at most
 * once per resolver, so one question mentioning three tasks costs one call.
 * Errors propagate to `resolveMentions`, which degrades them to raw tokens.
 */
export function createListMentionResolver(getJson: JsonGetter): MentionResolver {
  const lists = new Map<string, Promise<Row[]>>();
  const rowsOf = (url: string): Promise<Row[]> => {
    let pending = lists.get(url);
    if (!pending) {
      pending = getJson(url).then((data) =>
        Array.isArray(data) ? data.filter((row): row is Row => typeof row === 'object' && row !== null) : [],
      );
      lists.set(url, pending);
    }
    return pending;
  };
  return async (mention) => {
    const rows = await rowsOf(MENTION_SOURCES[mention.kind]);
    if (mention.kind === 'task') {
      const row = findTaskByKey(rows, mention.ref);
      return row ? summariseTask(row) : null;
    }
    if (mention.kind === 'repo') {
      const row = findRepoByName(rows, mention.ref);
      return row ? summariseRepo(row) : null;
    }
    const row = findIdeaById(rows, mention.ref);
    return row ? summariseIdea(row) : null;
  };
}

// ── Edit and resend ─────────────────────────────────────────────────────────

/** True when the last turn is a stopped answer to a user prompt. */
export function canEditAndResend(messages: readonly Pick<ConversationMessage, 'role' | 'stopped'>[]): boolean {
  const last = messages[messages.length - 1];
  const previous = messages[messages.length - 2];
  return Boolean(last && previous && last.role === 'assistant' && last.stopped && previous.role === 'user');
}

/**
 * Drop the stopped turn (prompt + partial answer) and hand the prompt back to
 * the composer. Null when the transcript does not end in a stopped turn.
 */
export function editAndResend<M extends Pick<ConversationMessage, 'role' | 'content' | 'stopped'>>(
  messages: readonly M[],
): { messages: M[]; prompt: string } | null {
  if (!canEditAndResend(messages)) return null;
  return { messages: messages.slice(0, -2), prompt: messages[messages.length - 2].content };
}

// ── Markdown export ─────────────────────────────────────────────────────────

export interface ExportableConversation {
  title: string;
  messages: readonly ConversationMessage[];
}

function fence(text: string, language = ''): string {
  // A fence longer than any backtick run inside keeps the block intact.
  const longestRun = Math.max(2, ...Array.from(text.matchAll(/`+/g), (m) => m[0].length));
  const ticks = '`'.repeat(longestRun + 1);
  return `${ticks}${language}\n${text}\n${ticks}`;
}

function actionStatus(action: ConversationMessage['actions'][number]): string {
  if (action.decided === 'approve') return 'approved';
  if (action.decided === 'reject') return 'rejected';
  return action.stale ? 'expired' : 'pending';
}

/** The whole conversation as Markdown, secrets masked, panel-local notes omitted. */
export function conversationToMarkdown(conversation: ExportableConversation, exportedAt: Date): string {
  const parts: string[] = [
    `# ${maskSecretsInText(conversation.title) || 'Ask Agent conversation'}`,
    '',
    `_Exported from DevServer Ask Agent on ${exportedAt.toISOString()}_`,
  ];
  for (const message of conversation.messages) {
    if (message.local) continue;
    parts.push('', message.role === 'user' ? '## You' : '## Agent', '');
    if (message.notices.length > 0) {
      parts.push(...message.notices.map((n) => `> ${maskSecretsInText(n)}`), '');
    }
    if (message.tools.length > 0) {
      const names = message.tools.map((t) => `\`${t.name.replace(/^mcp__DevServer__/, '')}\``).join(', ');
      parts.push(`_Lookups: ${names}_`, '');
    }
    parts.push(maskSecretsInText(message.content.trim()) || '_(no answer)_');
    if (message.stopped) parts.push('', '_(stopped)_');
    if (message.output) parts.push('', fence(maskSecretsInText(message.output)));
    for (const action of message.actions) {
      parts.push(
        '',
        `**Proposed action** \`${action.kind}\` (risk: ${action.risk}, ${actionStatus(action)}): ` +
          maskSecretsInText(action.summary),
      );
      if (Object.keys(action.params ?? {}).length > 0) {
        parts.push('', fence(JSON.stringify(maskParams(action.params), null, 2), 'json'));
      }
    }
    if (message.error) parts.push('', `> Error: ${maskSecretsInText(message.error)}`);
  }
  return `${parts.join('\n')}\n`;
}

/** `ask-agent-<slug>-YYYYMMDD-HHMM.md`, safe on every filesystem. */
export function exportFileName(title: string, exportedAt: Date): string {
  const slug =
    title
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/^-+|-+$/g, '')
      .slice(0, 40)
      .replace(/-+$/, '') || 'conversation';
  const pad = (n: number) => String(n).padStart(2, '0');
  const stamp =
    `${exportedAt.getFullYear()}${pad(exportedAt.getMonth() + 1)}${pad(exportedAt.getDate())}` +
    `-${pad(exportedAt.getHours())}${pad(exportedAt.getMinutes())}`;
  return `ask-agent-${slug}-${stamp}.md`;
}
