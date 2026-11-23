import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import {
  HISTORY_STORAGE_KEY,
  HISTORY_VERSION,
  MASK,
  MAX_CONVERSATIONS,
  createHistoryStore,
  deleteConversation,
  emptyHistory,
  isSecretKey,
  maskParams,
  maskSecretsInText,
  parseHistory,
  renameConversation,
  serializeHistory,
  upsertConversation,
  type ConversationMessage,
  type HistoryState,
  type StorageLike,
} from './ask-agent-history';

function message(partial: Partial<ConversationMessage> & Pick<ConversationMessage, 'role' | 'content'>): ConversationMessage {
  return { id: `m-${Math.random()}`, tools: [], notices: [], actions: [], ...partial };
}

function turn(question: string, answer = 'ok'): ConversationMessage[] {
  return [message({ role: 'user', content: question }), message({ role: 'assistant', content: answer })];
}

class MemoryStorage implements StorageLike {
  readonly values = new Map<string, string>();
  getItem(key: string): string | null {
    return this.values.get(key) ?? null;
  }
  setItem(key: string, value: string): void {
    this.values.set(key, value);
  }
}

describe('conversation list order and cap', () => {
  // Feature: F-ASK-01
  it('keeps at most 20 conversations and evicts the least recently updated', () => {
    let state: HistoryState = emptyHistory();
    for (let i = 0; i < MAX_CONVERSATIONS + 5; i += 1) {
      state = upsertConversation(state, { id: `c${i}`, messages: turn(`question ${i}`) }, 1000 + i);
    }
    assert.equal(state.conversations.length, MAX_CONVERSATIONS);
    assert.equal(state.conversations[0].id, 'c24');
    assert.equal(state.conversations.at(-1)?.id, 'c5');
    assert.ok(!state.conversations.some((c) => c.id === 'c0'));
  });

  // Feature: F-ASK-01
  it('moves a conversation to the front when it is updated again', () => {
    let state = emptyHistory();
    state = upsertConversation(state, { id: 'a', messages: turn('first') }, 1);
    state = upsertConversation(state, { id: 'b', messages: turn('second') }, 2);
    state = upsertConversation(state, { id: 'a', messages: [...turn('first'), ...turn('more')] }, 3);
    assert.deepEqual(
      state.conversations.map((c) => c.id),
      ['a', 'b'],
    );
    assert.equal(state.conversations[0].createdAt, 1);
    assert.equal(state.conversations[0].updatedAt, 3);
  });

  // Feature: F-ASK-01
  it('does not create an entry for an empty or local-only conversation', () => {
    const state = upsertConversation(
      emptyHistory(),
      { id: 'x', messages: [message({ role: 'assistant', content: 'help text', local: true })] },
      1,
    );
    assert.equal(state.conversations.length, 0);
  });

  // Feature: F-ASK-01
  it('keeps a custom title across later saves and reverts on a blank rename', () => {
    let state = upsertConversation(emptyHistory(), { id: 'a', messages: turn('why is the queue stuck?') }, 1);
    assert.equal(state.conversations[0].title, 'why is the queue stuck?');
    state = renameConversation(state, 'a', 'Queue debugging');
    state = upsertConversation(state, { id: 'a', messages: [...turn('why is the queue stuck?'), ...turn('and now?')] }, 2);
    assert.equal(state.conversations[0].title, 'Queue debugging');
    state = renameConversation(state, 'a', '   ');
    assert.equal(state.conversations[0].title, 'why is the queue stuck?');
    assert.equal(state.conversations[0].titleIsCustom, false);
  });

  // Feature: F-ASK-01
  it('deletes a conversation by id', () => {
    let state = upsertConversation(emptyHistory(), { id: 'a', messages: turn('one') }, 1);
    state = upsertConversation(state, { id: 'b', messages: turn('two') }, 2);
    state = deleteConversation(state, 'a');
    assert.deepEqual(
      state.conversations.map((c) => c.id),
      ['b'],
    );
  });
});

describe('secret masking before storage', () => {
  // Feature: F-ASK-02
  it('masks known token formats via the shared redaction rules', () => {
    const masked = maskSecretsInText('my key is ghp_abcdefghijklmnopqrstuvwxyz0123456789 ok');
    assert.ok(!masked.includes('ghp_abcdefghijklmnopqrstuvwxyz0123456789'));
    assert.match(masked, /REDACTED/);
  });

  // Feature: F-ASK-02
  it('masks key=value, key: value and JSON pairs whose key looks secret', () => {
    assert.equal(maskSecretsInText('GITEA_TOKEN=abc123'), `GITEA_TOKEN=${MASK}`);
    assert.equal(maskSecretsInText('api_key: hunter2'), `api_key: ${MASK}`);
    assert.equal(maskSecretsInText('{"password": "s3cret"}'), `{"password": "${MASK}"}`);
    assert.equal(maskSecretsInText('Authorization: Bearer abcdefghijkl'), `Authorization: Bearer ${MASK}`);
    assert.equal(maskSecretsInText('?page=2&access_token=xyz'), `?page=2&access_token=${MASK}`);
  });

  // Feature: F-ASK-02
  it('leaves non-secret keys such as max_tokens alone', () => {
    assert.equal(maskSecretsInText('max_tokens=4096 author: me'), 'max_tokens=4096 author: me');
    assert.equal(isSecretKey('apiKey'), true);
    assert.equal(isSecretKey('x-api-key'), true);
    assert.equal(isSecretKey('max_tokens'), false);
  });

  // Feature: F-ASK-02
  it('masks secret-named params recursively in proposals', () => {
    const masked = maskParams({ repo: 'web', env: { GITHUB_TOKEN: 'abc', dry_run: true }, apiKey: 42 });
    assert.deepEqual(masked, { repo: 'web', env: { GITHUB_TOKEN: MASK, dry_run: true }, apiKey: MASK });
  });

  // Feature: F-ASK-02
  it('never writes a secret from content, output or action params to storage', () => {
    const storage = new MemoryStorage();
    const store = createHistoryStore(storage);
    store.update((state) =>
      upsertConversation(
        state,
        {
          id: 'a',
          messages: [
            message({ role: 'user', content: 'use password=topsecret1' }),
            message({
              role: 'assistant',
              content: 'done',
              output: 'TOKEN=leakyvalue',
              actions: [
                { actionId: 'x', kind: 'run_script', summary: 's', params: { secret: 'p@ss' }, risk: 'low', decided: 'approve' },
              ],
            }),
          ],
        },
        1,
      ),
    );
    const raw = storage.getItem(HISTORY_STORAGE_KEY) ?? '';
    for (const secret of ['topsecret1', 'leakyvalue', 'p@ss']) assert.ok(!raw.includes(secret), secret);
  });
});

describe('reading stored history', () => {
  // Feature: F-ASK-03
  it('discards a different version without throwing', () => {
    const raw = JSON.stringify({ version: HISTORY_VERSION + 1, conversations: [{ id: 'a', messages: [] }] });
    assert.deepEqual(parseHistory(raw), emptyHistory());
  });

  // Feature: F-ASK-03
  it('discards corrupt JSON and a non-object payload', () => {
    assert.deepEqual(parseHistory('{not json'), emptyHistory());
    assert.deepEqual(parseHistory('[]'), emptyHistory());
    assert.deepEqual(parseHistory(null), emptyHistory());
  });

  // Feature: F-ASK-03
  it('overwrites a corrupt blob on the next save', () => {
    const storage = new MemoryStorage();
    storage.setItem(HISTORY_STORAGE_KEY, 'garbage');
    const store = createHistoryStore(storage);
    assert.deepEqual(store.load(), emptyHistory());
    store.update((state) => upsertConversation(state, { id: 'a', messages: turn('hello') }, 1));
    const reread = parseHistory(storage.getItem(HISTORY_STORAGE_KEY));
    assert.equal(reread.conversations.length, 1);
    assert.equal(reread.version, HISTORY_VERSION);
  });

  // Feature: F-ASK-03
  it('drops malformed entries but keeps valid ones', () => {
    const valid = upsertConversation(emptyHistory(), { id: 'ok', messages: turn('hi') }, 1);
    const raw = JSON.stringify({
      version: HISTORY_VERSION,
      conversations: [...valid.conversations, { id: 7 }, 'nope', { id: 'empty', messages: [{ role: 'robot' }] }],
    });
    assert.deepEqual(
      parseHistory(raw).conversations.map((c) => c.id),
      ['ok'],
    );
  });

  // Feature: F-ASK-03
  it('survives a storage that throws', () => {
    const store = createHistoryStore({
      getItem() {
        throw new Error('blocked');
      },
      setItem() {
        throw new Error('quota');
      },
    });
    assert.deepEqual(store.load(), emptyHistory());
    const next = store.update((state) => upsertConversation(state, { id: 'a', messages: turn('q') }, 1));
    assert.equal(next.conversations.length, 1);
  });
});

describe('what a stored conversation may contain', () => {
  // Feature: F-ASK-04
  it('stores an undecided proposal as stale and keeps decided ones', () => {
    const state = upsertConversation(
      emptyHistory(),
      {
        id: 'a',
        messages: [
          message({ role: 'user', content: 'restart please' }),
          message({
            role: 'assistant',
            content: 'proposed',
            actions: [
              { actionId: 'p1', kind: 'run_script', summary: 'restart', params: {}, risk: 'medium' },
              { actionId: 'p2', kind: 'cancel_task', summary: 'cancel', params: {}, risk: 'low', decided: 'reject' },
            ],
          }),
        ],
      },
      1,
    );
    const [pending, decided] = state.conversations[0].messages[1].actions;
    assert.equal(pending.stale, true);
    assert.equal(pending.decided, undefined);
    assert.equal(decided.decided, 'reject');
    assert.equal(decided.stale, undefined);
  });

  // Feature: F-ASK-04
  it('treats an undecided proposal on disk as stale even if the flag was lost', () => {
    const raw = JSON.stringify({
      version: HISTORY_VERSION,
      conversations: [
        {
          id: 'a',
          title: 't',
          updatedAt: 1,
          messages: [
            {
              id: 'm',
              role: 'assistant',
              content: 'x',
              actions: [{ actionId: 'p', kind: 'run_script', summary: '', params: {}, risk: 'high' }],
            },
          ],
        },
      ],
    });
    assert.equal(parseHistory(raw).conversations[0].messages[0].actions[0].stale, true);
  });

  // Feature: F-ASK-04
  it('never stores the worker session id', () => {
    const storage = new MemoryStorage();
    const snapshot = {
      id: 'a',
      sessionId: 'sess-SHOULD-NOT-PERSIST',
      session_id: 'sess-SHOULD-NOT-PERSIST',
      messages: turn('hello').map((m) => ({ ...m, sessionId: 'sess-SHOULD-NOT-PERSIST', streaming: true })),
    };
    createHistoryStore(storage).update((state) => upsertConversation(state, snapshot, 1));
    const raw = storage.getItem(HISTORY_STORAGE_KEY) ?? '';
    assert.ok(raw.length > 0);
    assert.ok(!raw.includes('sess-SHOULD-NOT-PERSIST'));
    assert.ok(!raw.includes('session'));
    assert.ok(!raw.includes('streaming'));
  });

  // Feature: F-ASK-04
  it('round-trips through serialize and parse', () => {
    const state = upsertConversation(emptyHistory(), { id: 'a', messages: turn('q', 'a') }, 5);
    assert.deepEqual(parseHistory(serializeHistory(state)), state);
  });
});
