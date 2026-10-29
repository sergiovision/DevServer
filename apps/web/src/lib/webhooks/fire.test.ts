import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { createWebhookHandler } from './handler';
import { signBody } from './signature';
import type { FiredTask, WebhookStore, WebhookTaskRequest, WebhookTrigger } from './fire';

const SECRET = 'top-secret';
const TOKEN = 'abc123';

function makeTrigger(overrides: Partial<WebhookTrigger> = {}): WebhookTrigger {
  return {
    id: 7,
    name: 'CI failures',
    repo_id: 3,
    template_id: null,
    secret: SECRET,
    title_template: 'Fix: {{job.name}}',
    description_template: 'Build {{build.number}} failed on {{name}}',
    task_key_prefix: 'CI',
    priority: 2,
    enabled: true,
    ...overrides,
  };
}

/** In-memory WebhookStore that records what the handler asked it to do. */
class FakeWebhookStore implements WebhookStore {
  createdTasks: WebhookTaskRequest[] = [];
  firings: { triggerId: number; task: FiredTask }[] = [];

  constructor(private readonly trigger: WebhookTrigger | null) {}

  async findTriggerByToken(token: string) {
    return token === TOKEN ? this.trigger : null;
  }

  async createAndEnqueueTask(request: WebhookTaskRequest) {
    this.createdTasks.push(request);
    return { taskId: 101, taskKey: request.taskKey };
  }

  async recordFiring(trigger: WebhookTrigger, task: FiredTask) {
    this.firings.push({ triggerId: trigger.id, task });
  }
}

const fixedClock = { now: new Date('2026-10-29T14:30:05Z'), randomSuffix: () => 'beef' };

function callHandler(store: WebhookStore, body: string, signature: string | null, token = TOKEN) {
  const handler = createWebhookHandler(store, fixedClock);
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (signature !== null) headers['X-DevServer-Signature'] = signature;
  const request = new Request(`http://localhost/api/webhooks/${token}`, { method: 'POST', headers, body });
  return handler(request, { params: Promise.resolve({ token }) });
}

describe('POST /api/webhooks/[token]', () => {
  const body = JSON.stringify({ job: { name: 'unit-tests' }, build: { number: 812 } });

  it('creates and enqueues a task when the signature is valid', async () => {
    const store = new FakeWebhookStore(makeTrigger());
    const res = await callHandler(store, body, signBody(body, SECRET));

    assert.equal(res.status, 201);
    assert.deepEqual(await res.json(), { taskId: 101, taskKey: 'CI-20261029-143005-beef' });

    assert.equal(store.createdTasks.length, 1);
    const created = store.createdTasks[0];
    assert.equal(created.title, 'Fix: unit-tests');
    assert.equal(created.description, 'Build 812 failed on CI failures');
    assert.equal(created.trigger.repo_id, 3);
    assert.deepEqual(store.firings, [{ triggerId: 7, task: { taskId: 101, taskKey: 'CI-20261029-143005-beef' } }]);
  });

  it('answers 401 and creates nothing when the signature is wrong', async () => {
    const store = new FakeWebhookStore(makeTrigger());
    const res = await callHandler(store, body, signBody(body, 'wrong-secret'));

    assert.equal(res.status, 401);
    assert.equal(store.createdTasks.length, 0);
    assert.equal(store.firings.length, 0);
  });

  it('answers 401 when the signature header is missing', async () => {
    const store = new FakeWebhookStore(makeTrigger());
    const res = await callHandler(store, body, null);
    assert.equal(res.status, 401);
  });

  it('answers 404 for a disabled trigger, even with a valid signature', async () => {
    const store = new FakeWebhookStore(makeTrigger({ enabled: false }));
    const res = await callHandler(store, body, signBody(body, SECRET));

    assert.equal(res.status, 404);
    assert.equal(store.createdTasks.length, 0);
  });

  it('answers 404 for an unknown token', async () => {
    const store = new FakeWebhookStore(makeTrigger());
    const res = await callHandler(store, body, signBody(body, SECRET), 'unknown');
    assert.equal(res.status, 404);
  });

  it('answers 400 for a correctly signed body that is not JSON', async () => {
    const store = new FakeWebhookStore(makeTrigger());
    const notJson = 'not json';
    const res = await callHandler(store, notJson, signBody(notJson, SECRET));

    assert.equal(res.status, 400);
    assert.equal(store.createdTasks.length, 0);
  });

  it('never fires a trigger that has no secret', async () => {
    const store = new FakeWebhookStore(makeTrigger({ secret: null }));
    const res = await callHandler(store, body, signBody(body, ''));
    assert.equal(res.status, 401);
  });
});
