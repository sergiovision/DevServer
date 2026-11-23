/**
 * Specification for the browser JSON client and the stale-response gate.
 */
import { afterEach, describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { ApiError, api, messageFromErrorBody } from './api-client';
import { createLatestRequestGate } from './use-async';

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
});

describe('api client', () => {
  // Feature: F-WEB-01
  it('decodes a JSON success body', async () => {
    globalThis.fetch = (async () => new Response('{"id":7}', { status: 200 })) as typeof fetch;
    assert.deepEqual(await api.get('/api/tasks/7'), { id: 7 });
  });

  // Feature: F-WEB-01
  it('throws ApiError with the route message, status and detail', async () => {
    globalThis.fetch = (async () =>
      new Response('{"error":"Task not found","detail":"id=7"}', { status: 404 })) as typeof fetch;
    await assert.rejects(api.post('/api/tasks/7/run'), (err: unknown) => {
      assert.ok(err instanceof ApiError);
      assert.equal(err.status, 404);
      assert.equal(err.message, 'Task not found');
      assert.equal(err.detail, 'id=7');
      return true;
    });
  });

  // Feature: F-WEB-01
  it('a rejected fetch becomes a network ApiError with status 0', async () => {
    globalThis.fetch = (async () => {
      throw new TypeError('Failed to fetch');
    }) as typeof fetch;
    await assert.rejects(api.get('/api/x'), (err: unknown) => err instanceof ApiError && err.isNetworkError);
  });

  // Feature: F-WEB-01
  it('an empty 2xx body resolves to null', async () => {
    globalThis.fetch = (async () => new Response(null, { status: 204 })) as typeof fetch;
    assert.equal(await api.delete('/api/x'), null);
  });

  // Feature: F-WEB-01
  it('sends a JSON body with a content type', async () => {
    let seen: RequestInit | undefined;
    globalThis.fetch = (async (_url: string, init?: RequestInit) => {
      seen = init;
      return new Response('{}', { status: 200 });
    }) as typeof fetch;
    await api.patch('/api/tasks/1', { title: 't' });
    assert.equal(seen?.method, 'PATCH');
    assert.equal(seen?.body, '{"title":"t"}');
    assert.equal((seen?.headers as Record<string, string>)['Content-Type'], 'application/json');
  });

  // Feature: F-WEB-01
  it('falls back to FastAPI detail and then a status message', () => {
    assert.equal(messageFromErrorBody({ detail: 'nope' }, 422), 'nope');
    assert.equal(messageFromErrorBody(null, 503), 'Request failed (503)');
  });
});

describe('createLatestRequestGate', () => {
  // Feature: F-WEB-02
  it('a stale response is ignored once a newer request started', () => {
    const gate = createLatestRequestGate();
    const first = gate.next();
    const second = gate.next();
    assert.equal(gate.isCurrent(first), false);
    assert.equal(gate.isCurrent(second), true);
  });

  // Feature: F-WEB-02
  it('nothing is current after close (no setState after unmount)', () => {
    const gate = createLatestRequestGate();
    const ticket = gate.next();
    gate.close();
    assert.equal(gate.isCurrent(ticket), false);
  });
});
