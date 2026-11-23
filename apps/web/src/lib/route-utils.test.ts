/**
 * Specification for the route-handler plumbing in `route-utils.ts`.
 * `fetch` is replaced per test so no worker needs to run.
 */
import { afterEach, describe, it } from 'node:test';
import assert from 'node:assert/strict';
import {
  invalidId,
  jsonError,
  parseRouteId,
  proxyWorker,
  workerError,
  workerErrorMessage,
} from './route-utils';

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
});

function fakeWorker(status: number, body: unknown): void {
  globalThis.fetch = (async () =>
    new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })) as typeof fetch;
}

describe('parseRouteId', () => {
  // Feature: F-WEB-03
  it('accepts a positive integer', () => {
    assert.equal(parseRouteId('42'), 42);
  });

  // Feature: F-WEB-03
  it('rejects ids that would reach SQL as NaN or a wrong row', () => {
    for (const raw of ['abc', '12abc', '-1', '0', '1.5', '', ' ', undefined, null]) {
      assert.equal(parseRouteId(raw as string), null, `expected ${String(raw)} to be rejected`);
    }
  });
});

describe('invalidId / jsonError', () => {
  // Feature: F-WEB-03
  it('a bad [id] answers 400 not 500', async () => {
    const res = invalidId();
    assert.equal(res.status, 400);
    assert.deepEqual(await res.json(), { error: 'Invalid id' });
  });

  // Feature: F-WEB-03
  it('jsonError omits detail when there is none', async () => {
    assert.deepEqual(await jsonError(404, 'Task not found').json(), { error: 'Task not found' });
    assert.deepEqual(await jsonError(409, 'Busy', { lock: 'repo' }).json(), {
      error: 'Busy',
      detail: { lock: 'repo' },
    });
  });
});

describe('proxyWorker', () => {
  // Feature: F-WEB-04
  it('relays a successful worker body with its status', async () => {
    fakeWorker(200, { ok: true, n: 3 });
    const res = await proxyWorker('/internal/x');
    assert.equal(res.status, 200);
    assert.deepEqual(await res.json(), { ok: true, n: 3 });
  });

  // Feature: F-WEB-04
  it('forwards the upstream error detail and status', async () => {
    fakeWorker(404, { detail: 'task DS-1 not found' });
    const res = await proxyWorker('/internal/tasks/DS-1/continue', { method: 'POST' });
    assert.equal(res.status, 404);
    assert.deepEqual(await res.json(), { error: 'task DS-1 not found' });
  });

  // Feature: F-WEB-04
  it('answers 502 "Worker unreachable" when the worker does not answer', async () => {
    globalThis.fetch = (async () => {
      throw new TypeError('fetch failed');
    }) as typeof fetch;
    const res = await proxyWorker('/internal/x');
    assert.equal(res.status, 502);
    assert.deepEqual(await res.json(), { error: 'Worker unreachable' });
  });

  // Feature: F-WEB-04
  it('applies the transform only to successful bodies', async () => {
    fakeWorker(200, { items: [1, 2] });
    const res = await proxyWorker('/internal/x', { transform: (b) => ({ count: (b.items as unknown[]).length }) });
    assert.deepEqual(await res.json(), { count: 2 });
  });
});

describe('proxyWorker request and status contract', () => {
  // Feature: F-WEB-04
  it('answers a success with successStatus when the route contract differs from the worker', async () => {
    fakeWorker(200, { id: 7 });
    const res = await proxyWorker('/internal/schedules', { method: 'POST', body: {}, successStatus: 201 });
    assert.equal(res.status, 201);
    assert.deepEqual(await res.json(), { id: 7 });
  });

  // Feature: F-WEB-04
  it('keeps the worker error status even when successStatus is set', async () => {
    fakeWorker(422, { detail: 'cron expression invalid' });
    const res = await proxyWorker('/internal/schedules', { method: 'POST', body: {}, successStatus: 201 });
    assert.equal(res.status, 422);
    assert.deepEqual(await res.json(), { error: 'cron expression invalid' });
  });

  // Feature: F-WEB-04
  it('sends the body as JSON with the requested method', async () => {
    let seen: { url: string; method?: string; contentType?: string; body?: string } | null = null;
    globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
      seen = {
        url: String(input),
        method: init?.method,
        contentType: (init?.headers as Record<string, string> | undefined)?.['Content-Type'],
        body: typeof init?.body === 'string' ? init.body : undefined,
      };
      return new Response('{}', { status: 200 });
    }) as typeof fetch;
    await proxyWorker('/internal/decisions/3/resolve', { method: 'POST', body: { decision: 'approve' } });
    assert.ok(seen);
    const call = seen as { url: string; method?: string; contentType?: string; body?: string };
    assert.ok(call.url.endsWith('/internal/decisions/3/resolve'));
    assert.equal(call.method, 'POST');
    assert.equal(call.contentType, 'application/json');
    assert.deepEqual(JSON.parse(call.body ?? 'null'), { decision: 'approve' });
  });
});

describe('workerError / workerErrorMessage', () => {
  // Feature: F-WEB-04
  it('relays a failed worker response with its status and detail', async () => {
    const upstream = new Response(null, { status: 409 });
    const res = workerError(upstream, { detail: 'session is live' });
    assert.equal(res.status, 409);
    assert.deepEqual(await res.json(), { error: 'session is live' });
  });

  // Feature: F-WEB-04
  it('names the status when the worker gave no message and serialises structured detail', () => {
    assert.equal(workerErrorMessage({}, 500), 'Worker returned 500');
    assert.equal(workerErrorMessage({ error: 'boom' }, 500), 'boom');
    assert.equal(
      workerErrorMessage({ detail: [{ loc: ['body', 'name'], msg: 'field required' }] }, 422),
      '[{"loc":["body","name"],"msg":"field required"}]',
    );
  });
});
