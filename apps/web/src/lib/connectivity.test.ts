import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import {
  DEGRADED_POLL_MS,
  HEALTHY_POLL_MS,
  getConnectivityStatus,
  initialConnectivityState,
  isConnectivityProbeUrl,
  next,
  pollIntervalMs,
  setConnectivityStatus,
  shouldSuppressToast,
  subscribeConnectivityStatus,
  type ConnectivityInput,
  type ConnectivityMachineState,
} from './connectivity';

/** Feed inputs in order; collect every `restored` flag. */
function run(start: ConnectivityMachineState, inputs: ConnectivityInput[]) {
  let state = start;
  const restoredFlags: boolean[] = [];
  for (const input of inputs) {
    const result = next(state, input);
    state = result.state;
    restoredFlags.push(result.restored);
  }
  return { state, restoredFlags };
}

const HEALTHY: ConnectivityInput = { kind: 'probe-answered', worker: true };
const WORKER_DOWN: ConnectivityInput = { kind: 'probe-answered', worker: false };
const THREW: ConnectivityInput = { kind: 'probe-threw' };

describe('connectivity state transitions', () => {
  // Feature: F-CONN-01
  it('goes offline immediately on a browser offline event', () => {
    const { state } = run(initialConnectivityState(), [{ kind: 'browser-offline' }]);
    assert.equal(state.status, 'offline');
  });

  // Feature: F-CONN-01
  it('needs two consecutive probe failures before leaving online', () => {
    const first = run(initialConnectivityState(), [THREW]);
    assert.equal(first.state.status, 'online');
    const second = run(first.state, [THREW]);
    assert.equal(second.state.status, 'server-down');
  });

  // Feature: F-CONN-01
  it('a healthy probe between failures resets the debounce', () => {
    const { state } = run(initialConnectivityState(), [THREW, HEALTHY, THREW]);
    assert.equal(state.status, 'online');
  });

  // Feature: F-CONN-01
  it('reports worker-down when the server answers but the worker does not', () => {
    const { state } = run(initialConnectivityState(), [WORKER_DOWN, WORKER_DOWN]);
    assert.equal(state.status, 'worker-down');
  });

  // Feature: F-CONN-01
  it('treats a non-OK probe answer as server-down', () => {
    const notOk: ConnectivityInput = { kind: 'probe-not-ok', httpStatus: 502 };
    const { state } = run(initialConnectivityState(), [notOk, notOk]);
    assert.equal(state.status, 'server-down');
  });

  // Feature: F-CONN-01
  it('switches failure kind immediately once already degraded', () => {
    const { state } = run(initialConnectivityState(), [WORKER_DOWN, WORKER_DOWN, THREW]);
    assert.equal(state.status, 'server-down');
  });

  // Feature: F-CONN-01
  it('stays offline after browser online until a probe confirms the server', () => {
    const { state } = run(initialConnectivityState(), [
      { kind: 'browser-offline' },
      { kind: 'browser-online' },
    ]);
    assert.equal(state.status, 'offline');
    assert.equal(next(state, HEALTHY).state.status, 'online');
  });

  // Feature: F-CONN-01
  it('a probe that throws while the browser is offline keeps the offline status', () => {
    const { state } = run(initialConnectivityState(), [{ kind: 'browser-offline' }, THREW, THREW]);
    assert.equal(state.status, 'offline');
  });

  // Feature: F-CONN-01
  it('polls slowly while healthy and fast while degraded or unconfirmed', () => {
    const healthy = initialConnectivityState();
    assert.equal(pollIntervalMs(healthy), HEALTHY_POLL_MS);
    assert.equal(pollIntervalMs(next(healthy, THREW).state), DEGRADED_POLL_MS);
    assert.equal(pollIntervalMs(run(healthy, [THREW, THREW]).state), DEGRADED_POLL_MS);
  });
});

describe('connectivity restored signal', () => {
  // Feature: F-CONN-02
  it('fires exactly once when a degraded status returns to online', () => {
    const { restoredFlags } = run(initialConnectivityState(), [
      THREW,
      THREW,
      HEALTHY,
      HEALTHY,
      HEALTHY,
    ]);
    assert.deepEqual(restoredFlags, [false, false, true, false, false]);
  });

  // Feature: F-CONN-02
  it('does not fire for healthy probes that never left online', () => {
    const { restoredFlags } = run(initialConnectivityState(), [HEALTHY, THREW, HEALTHY]);
    assert.ok(restoredFlags.every((flag) => !flag));
  });

  // Feature: F-CONN-02
  it('fires once per outage across two separate outages', () => {
    const { restoredFlags } = run(initialConnectivityState(), [
      { kind: 'browser-offline' },
      { kind: 'browser-online' },
      HEALTHY,
      WORKER_DOWN,
      WORKER_DOWN,
      HEALTHY,
      HEALTHY,
    ]);
    assert.equal(restoredFlags.filter(Boolean).length, 2);
  });
});

describe('shouldSuppressToast', () => {
  // Feature: F-CONN-03
  it('never suppresses while online', () => {
    assert.equal(shouldSuppressToast('online', { status: 0, isNetworkError: true }), false);
    assert.equal(shouldSuppressToast('online', { status: 502, isNetworkError: false }), false);
  });

  // Feature: F-CONN-03
  it('suppresses network failures in every degraded status', () => {
    for (const status of ['offline', 'server-down', 'worker-down'] as const) {
      assert.equal(shouldSuppressToast(status, { status: 0, isNetworkError: true }), true);
    }
  });

  // Feature: F-CONN-03
  it('suppresses worker-unreachable 502 while the worker is down but not other errors', () => {
    assert.equal(shouldSuppressToast('worker-down', { status: 502, isNetworkError: false }), true);
    assert.equal(shouldSuppressToast('worker-down', { status: 500, isNetworkError: false }), false);
    assert.equal(shouldSuppressToast('worker-down', { status: 404, isNetworkError: false }), false);
  });

  // Feature: F-CONN-03
  it('suppresses gateway errors while the server is down but keeps validation errors', () => {
    assert.equal(shouldSuppressToast('server-down', { status: 504, isNetworkError: false }), true);
    assert.equal(shouldSuppressToast('server-down', { status: 422, isNetworkError: false }), false);
  });

  // Feature: F-CONN-03
  it('recognises the probe URL so the interceptor can skip it', () => {
    assert.equal(isConnectivityProbeUrl('/api/health/connectivity'), true);
    assert.equal(isConnectivityProbeUrl('http://host:3200/api/health/connectivity?t=1'), true);
    assert.equal(isConnectivityProbeUrl('/api/health'), false);
  });
});

describe('connectivity status store', () => {
  // Feature: F-CONN-03
  it('publishes status changes to subscribers and only on change', () => {
    const seen: string[] = [];
    const unsubscribe = subscribeConnectivityStatus((status) => seen.push(status));
    setConnectivityStatus('worker-down');
    setConnectivityStatus('worker-down');
    assert.equal(getConnectivityStatus(), 'worker-down');
    setConnectivityStatus('online');
    unsubscribe();
    setConnectivityStatus('offline');
    setConnectivityStatus('online');
    assert.deepEqual(seen, ['worker-down', 'online']);
  });
});
