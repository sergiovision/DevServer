import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import {
  connectivityListenerCount,
  emitConnectivityRestored,
  onConnectivityRestored,
} from './connectivity-events';

describe('connectivity restored events', () => {
  // Feature: F-CONN-04
  it('calls every subscribed panel exactly once per emit', () => {
    let calls = 0;
    const unsubscribe = onConnectivityRestored(() => {
      calls += 1;
    });
    emitConnectivityRestored();
    assert.equal(calls, 1);
    unsubscribe();
  });

  // Feature: F-CONN-04
  it('stops calling a panel after it unsubscribes', () => {
    const before = connectivityListenerCount();
    let calls = 0;
    const unsubscribe = onConnectivityRestored(() => {
      calls += 1;
    });
    assert.equal(connectivityListenerCount(), before + 1);
    unsubscribe();
    assert.equal(connectivityListenerCount(), before);
    emitConnectivityRestored();
    assert.equal(calls, 0);
  });

  // Feature: F-CONN-04
  it('a throwing listener does not stop the other panels from reloading', () => {
    let reloaded = 0;
    const unsubscribeThrowing = onConnectivityRestored(() => {
      throw new Error('panel exploded');
    });
    const unsubscribeHealthy = onConnectivityRestored(() => {
      reloaded += 1;
    });
    assert.doesNotThrow(() => emitConnectivityRestored());
    assert.equal(reloaded, 1);
    unsubscribeThrowing();
    unsubscribeHealthy();
  });
});
