import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import {
  IDLE_RECALL,
  canEditAndResend,
  conversationToMarkdown,
  createListMentionResolver,
  decideComposerKey,
  editAndResend,
  exportFileName,
  filterSlashCommands,
  findTaskByKey,
  parseMentions,
  parseSlashCommand,
  recallNext,
  recallPrevious,
  resolveMentions,
  slashQuery,
  summariseRepo,
  summariseTask,
  type ComposerKeyInput,
  type Mention,
} from './ask-agent-composer';
import type { ConversationMessage } from './ask-agent-history';

function key(partial: Partial<ComposerKeyInput> & Pick<ComposerKeyInput, 'key'>): ComposerKeyInput {
  const value = partial.value ?? '';
  return {
    shiftKey: false,
    isComposing: false,
    value,
    selectionStart: value.length,
    selectionEnd: value.length,
    slashMenuOpen: false,
    recalling: false,
    ...partial,
  };
}

function message(partial: Partial<ConversationMessage> & Pick<ConversationMessage, 'role' | 'content'>): ConversationMessage {
  return { id: `m-${Math.random()}`, tools: [], notices: [], actions: [], ...partial };
}

describe('composer key decisions', () => {
  // Feature: F-ASK-05
  it('sends on Enter and inserts a newline on Shift+Enter', () => {
    assert.equal(decideComposerKey(key({ key: 'Enter', value: 'hi' })), 'send');
    assert.equal(decideComposerKey(key({ key: 'Enter', value: 'hi', shiftKey: true })), 'newline');
  });

  // Feature: F-ASK-05
  it('never acts while an IME composition is in progress', () => {
    assert.equal(decideComposerKey(key({ key: 'Enter', value: 'にほ', isComposing: true })), 'default');
  });

  // Feature: F-ASK-05
  it('recalls the previous prompt on ArrowUp only from an empty input or the very start', () => {
    assert.equal(decideComposerKey(key({ key: 'ArrowUp', value: '' })), 'recall-previous');
    assert.equal(
      decideComposerKey(key({ key: 'ArrowUp', value: 'line one\nline two', selectionStart: 0, selectionEnd: 0 })),
      'recall-previous',
    );
    assert.equal(decideComposerKey(key({ key: 'ArrowUp', value: 'line one\nline two' })), 'default');
  });

  // Feature: F-ASK-05
  it('goes forward on ArrowDown only while recalling and at the end', () => {
    assert.equal(decideComposerKey(key({ key: 'ArrowDown', value: 'old', recalling: true })), 'recall-next');
    assert.equal(decideComposerKey(key({ key: 'ArrowDown', value: 'old', recalling: false })), 'default');
    assert.equal(
      decideComposerKey(
        key({ key: 'ArrowDown', value: 'old\nlines', recalling: true, selectionStart: 0, selectionEnd: 0 }),
      ),
      'default',
    );
  });

  // Feature: F-ASK-05
  it('keeps walking back on ArrowUp while recalling even with the caret at the end', () => {
    assert.equal(decideComposerKey(key({ key: 'ArrowUp', value: 'recalled', recalling: true })), 'recall-previous');
    assert.equal(decideComposerKey(key({ key: 'ArrowUp', value: 'recalled', recalling: false })), 'default');
  });

  // Feature: F-ASK-05
  it('steers the slash menu with arrows, Enter, Tab and Escape while it is open', () => {
    const open = { value: '/', slashMenuOpen: true };
    assert.equal(decideComposerKey(key({ key: 'ArrowUp', ...open })), 'menu-up');
    assert.equal(decideComposerKey(key({ key: 'ArrowDown', ...open })), 'menu-down');
    assert.equal(decideComposerKey(key({ key: 'Enter', ...open })), 'menu-select');
    assert.equal(decideComposerKey(key({ key: 'Tab', ...open })), 'menu-select');
    assert.equal(decideComposerKey(key({ key: 'Escape', ...open })), 'menu-close');
  });

  // Feature: F-ASK-05
  it('walks back through prompts and restores the draft when walking forward past the newest', () => {
    const prompts = ['first', 'second'];
    let step = recallPrevious(prompts, IDLE_RECALL, 'draft text');
    assert.equal(step.value, 'second');
    step = recallPrevious(prompts, step.state, step.value);
    assert.equal(step.value, 'first');
    step = recallPrevious(prompts, step.state, step.value);
    assert.equal(step.value, 'first');
    step = recallNext(prompts, step.state, step.value);
    assert.equal(step.value, 'second');
    step = recallNext(prompts, step.state, step.value);
    assert.equal(step.value, 'draft text');
    assert.equal(step.state.index, null);
  });

  // Feature: F-ASK-05
  it('leaves the input alone when there is nothing to recall', () => {
    assert.deepEqual(recallPrevious([], IDLE_RECALL, 'typed'), { state: IDLE_RECALL, value: 'typed' });
    assert.deepEqual(recallNext(['a'], IDLE_RECALL, 'typed'), { state: IDLE_RECALL, value: 'typed' });
  });
});

describe('mentions', () => {
  // Feature: F-ASK-06
  it('parses task keys, repo names and idea ids but not e-mail addresses', () => {
    const mentions = parseMentions('Why did @ds-42 fail on @repo:web-app. See @idea:7 and mail me@example.com');
    assert.deepEqual(mentions, [
      { kind: 'task', ref: 'DS-42', raw: '@ds-42' },
      { kind: 'repo', ref: 'web-app', raw: '@repo:web-app' },
      { kind: 'idea', ref: '7', raw: '@idea:7' },
    ]);
  });

  // Feature: F-ASK-06
  it('deduplicates repeated mentions', () => {
    assert.equal(parseMentions('@DS-1 and again @ds-1').length, 1);
  });

  // Feature: F-ASK-06
  it('appends a context block for resolved mentions', async () => {
    const result = await resolveMentions('Why did @DS-42 fail?', async (mention: Mention) =>
      mention.kind === 'task' ? `Task: ${mention.ref} — Fix login\nStatus: failed` : null,
    );
    assert.equal(result.resolved.length, 1);
    assert.ok(result.message.startsWith('Why did @DS-42 fail?\n\n---\nReferenced context:'));
    assert.match(result.message, /### @DS-42 \(task\)\nTask: DS-42 — Fix login/);
    assert.equal(result.contextBlock, result.message.slice('Why did @DS-42 fail?\n\n'.length));
  });

  // Feature: F-ASK-06
  it('leaves the raw @token and sends the question unchanged when resolution fails', async () => {
    const question = 'Status of @DS-9 in @repo:web?';
    const result = await resolveMentions(question, async (mention: Mention) => {
      if (mention.kind === 'task') throw new Error('database is down');
      return null;
    });
    assert.equal(result.message, question);
    assert.equal(result.contextBlock, null);
    assert.equal(result.unresolved.length, 2);
  });

  // Feature: F-ASK-06
  it('resolves through the list endpoints, fetching each list once', async () => {
    const calls: string[] = [];
    const resolver = createListMentionResolver(async (url) => {
      calls.push(url);
      if (url.startsWith('/api/tasks')) return [{ task_key: 'DS-1', title: 'One' }, { task_key: 'DS-2', title: 'Two' }];
      if (url === '/api/repos') return [{ name: 'web', provider: 'local' }];
      return [{ id: 7, title: 'Big idea', content: 'details' }];
    });
    const result = await resolveMentions('@DS-1 @DS-2 @DS-404 @repo:Web @idea:7', resolver);
    assert.deepEqual(
      result.resolved.map((m) => m.raw),
      ['@DS-1', '@DS-2', '@repo:Web', '@idea:7'],
    );
    assert.deepEqual(
      result.unresolved.map((m) => m.raw),
      ['@DS-404'],
    );
    assert.equal(calls.filter((url) => url.startsWith('/api/tasks')).length, 1);
    assert.match(result.message, /Idea: #7 — Big idea/);
  });

  // Feature: F-ASK-06
  it('summarises rows from the existing list endpoints without credentials', () => {
    const task = findTaskByKey([{ task_key: 'DS-42', title: 'Fix login', status: 'failed', repo_name: 'web' }], 'ds-42');
    assert.ok(task);
    assert.match(summariseTask(task), /Task: DS-42 — Fix login\nStatus: failed/);
    const repo = summariseRepo({ name: 'web', provider: 'github', gitea_token: 'ghp_secret', default_branch: 'main' });
    assert.ok(!repo.includes('ghp_secret'));
    assert.match(repo, /Repo: web/);
  });
});

describe('markdown export', () => {
  // Feature: F-ASK-07
  it('renders turns, lookups and proposals with secrets masked', () => {
    const markdown = conversationToMarkdown(
      {
        title: 'Restart help',
        messages: [
          message({ role: 'user', content: 'restart with TOKEN=abc123' }),
          message({ role: 'assistant', content: '/help output', local: true }),
          message({
            role: 'assistant',
            content: 'I can restart it.',
            tools: [{ id: 't', name: 'mcp__DevServer__task_list', ok: true }],
            actions: [
              { actionId: 'a', kind: 'run_script', summary: 'restart', params: { password: 'pw' }, risk: 'high', stale: true },
            ],
          }),
        ],
      },
      new Date('2026-01-02T03:04:05Z'),
    );
    assert.ok(markdown.startsWith('# Restart help\n'));
    assert.match(markdown, /## You\n\nrestart with TOKEN=\[REDACTED\]/);
    assert.match(markdown, /## Agent\n\n_Lookups: `task_list`_\n\nI can restart it\./);
    assert.match(markdown, /\*\*Proposed action\*\* `run_script` \(risk: high, expired\): restart/);
    assert.ok(!markdown.includes('"pw"'));
    assert.ok(!markdown.includes('/help output'));
    assert.ok(!markdown.includes('abc123'));
  });

  // Feature: F-ASK-07
  it('builds a filesystem-safe file name', () => {
    const name = exportFileName('Why is DS-42 / queue stuck?!', new Date(2026, 0, 2, 3, 4));
    assert.equal(name, 'ask-agent-why-is-ds-42-queue-stuck-20260102-0304.md');
    assert.equal(exportFileName('???', new Date(2026, 0, 2, 3, 4)), 'ask-agent-conversation-20260102-0304.md');
  });
});

describe('slash command menu', () => {
  // Feature: F-ASK-08
  it('opens only while a command word is being typed', () => {
    assert.equal(slashQuery('/'), '');
    assert.equal(slashQuery('/Ex'), 'ex');
    assert.equal(slashQuery('/export now'), null);
    assert.equal(slashQuery('hello /new'), null);
  });

  // Feature: F-ASK-08
  it('filters by prefix first, then substring', () => {
    assert.deepEqual(
      filterSlashCommands('').map((c) => c.name),
      ['new', 'clear', 'export', 'history', 'help'],
    );
    assert.deepEqual(
      filterSlashCommands('h').map((c) => c.name),
      ['history', 'help'],
    );
    assert.deepEqual(
      filterSlashCommands('e').map((c) => c.name),
      ['export', 'new', 'clear', 'help'],
    );
    assert.deepEqual(filterSlashCommands('zzz'), []);
  });

  // Feature: F-ASK-08
  it('parses only complete, known commands', () => {
    assert.equal(parseSlashCommand(' /Export '), 'export');
    assert.equal(parseSlashCommand('/nope'), null);
    assert.equal(parseSlashCommand('/new please'), null);
  });
});

describe('edit and resend', () => {
  // Feature: F-ASK-09
  it('drops the stopped turn and returns its prompt to the composer', () => {
    const messages = [
      message({ role: 'user', content: 'first' }),
      message({ role: 'assistant', content: 'answer' }),
      message({ role: 'user', content: 'second question' }),
      message({ role: 'assistant', content: 'partial…', stopped: true }),
    ];
    const result = editAndResend(messages);
    assert.ok(result);
    assert.equal(result.prompt, 'second question');
    assert.deepEqual(result.messages, messages.slice(0, 2));
  });

  // Feature: F-ASK-09
  it('is unavailable unless the last answer was stopped', () => {
    const finished = [message({ role: 'user', content: 'q' }), message({ role: 'assistant', content: 'a' })];
    assert.equal(canEditAndResend(finished), false);
    assert.equal(editAndResend(finished), null);
    assert.equal(canEditAndResend([]), false);
  });
});
