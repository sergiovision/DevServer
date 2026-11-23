import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import {
  createNeedsYouPoll,
  needsYouHref,
  parseNeedsYouCounts,
  totalNeedsYou,
  type NeedsYouCounts,
  type NeedsYouPollDependencies,
} from './needs-you';

/** Deterministic stand-ins for setInterval, fetch and document.hidden. */
function makeHarness(counts: NeedsYouCounts = { blocked: 2, plans_awaiting_decision: 1 }) {
  const timers = new Map<number, () => void>();
  let nextHandle = 1;
  const harness = {
    fetchCalls: 0,
    timersStarted: 0,
    hidden: false,
    failNext: false,
    counts,
    timers,
    /** Fire every live timer once, as one interval tick would. */
    tick() {
      for (const callback of [...timers.values()]) callback();
    },
  };
  const dependencies: NeedsYouPollDependencies = {
    fetchCounts: async () => {
      harness.fetchCalls += 1;
      if (harness.failNext) {
        harness.failNext = false;
        throw new Error('network down');
      }
      return harness.counts;
    },
    startTimer: (callback) => {
      harness.timersStarted += 1;
      const handle = nextHandle++;
      timers.set(handle, callback);
      return handle;
    },
    stopTimer: (handle) => {
      timers.delete(handle as number);
    },
    isHidden: () => harness.hidden,
    intervalMs: 15_000,
  };
  return { harness, poll: createNeedsYouPoll(dependencies) };
}

/** Let the fetch promise and its listener fan-out settle. */
const settle = () => new Promise<void>((resolve) => setImmediate(resolve));

describe('createNeedsYouPoll', () => {
  // Feature: F-NEED-02
  it('runs one timer and one fetch per tick for any number of subscribers', async () => {
    const { harness, poll } = makeHarness();
    const received: NeedsYouCounts[][] = [[], [], []];
    const unsubscribers = received.map((bucket) => poll.subscribe((counts) => bucket.push(counts)));
    await settle();

    assert.equal(harness.timersStarted, 1);
    assert.equal(harness.timers.size, 1);
    assert.equal(harness.fetchCalls, 1, 'first subscriber triggers one immediate fetch');

    harness.tick();
    await settle();
    assert.equal(harness.fetchCalls, 2);
    for (const bucket of received) assert.equal(bucket.length, 2);

    for (const unsubscribe of unsubscribers) unsubscribe();
  });

  // Feature: F-NEED-02
  it('stops the timer when the last subscriber leaves, and restarts on the next', async () => {
    const { harness, poll } = makeHarness();
    const first = poll.subscribe(() => {});
    const second = poll.subscribe(() => {});
    first();
    assert.equal(harness.timers.size, 1, 'one subscriber still listening');
    second();
    assert.equal(harness.timers.size, 0);
    second(); // idempotent
    assert.equal(harness.timers.size, 0);

    await settle();
    const fetchesBefore = harness.fetchCalls;
    harness.tick();
    await settle();
    assert.equal(harness.fetchCalls, fetchesBefore, 'no timer, no fetch');

    const third = poll.subscribe(() => {});
    assert.equal(harness.timersStarted, 2);
    third();
  });

  // Feature: F-NEED-02
  it('hands a late subscriber the last counts immediately', async () => {
    const { poll } = makeHarness();
    const first = poll.subscribe(() => {});
    await settle();
    const late: NeedsYouCounts[] = [];
    const second = poll.subscribe((counts) => late.push(counts));
    assert.deepEqual(late, [{ blocked: 2, plans_awaiting_decision: 1 }]);
    first();
    second();
  });

  // Feature: F-NEED-02
  it('does not overlap fetches when ticks outpace the server', async () => {
    const { harness, poll } = makeHarness();
    const unsubscribe = poll.subscribe(() => {});
    harness.tick();
    harness.tick();
    await settle();
    assert.equal(harness.fetchCalls, 1);
    unsubscribe();
  });

  // Feature: F-NEED-03
  it('skips ticks while the document is hidden and resumes when visible', async () => {
    const { harness, poll } = makeHarness();
    harness.hidden = true;
    const unsubscribe = poll.subscribe(() => {});
    await settle();
    assert.equal(harness.fetchCalls, 0, 'no immediate fetch for a hidden tab');
    harness.tick();
    await settle();
    assert.equal(harness.fetchCalls, 0);

    harness.hidden = false;
    harness.tick();
    await settle();
    assert.equal(harness.fetchCalls, 1);
    unsubscribe();
  });

  // Feature: F-NEED-02
  it('keeps the last known counts when a fetch fails', async () => {
    const { harness, poll } = makeHarness();
    const seen: NeedsYouCounts[] = [];
    const unsubscribe = poll.subscribe((counts) => seen.push(counts));
    await settle();
    harness.failNext = true;
    harness.tick();
    await settle();
    assert.equal(seen.length, 1);
    assert.deepEqual(poll.current(), { blocked: 2, plans_awaiting_decision: 1 });
    unsubscribe();
  });
});

describe('needs-you counts', () => {
  // Feature: F-NEED-01
  it('totals blocked tasks and plans awaiting a decision', () => {
    assert.equal(totalNeedsYou({ blocked: 2, plans_awaiting_decision: 3 }), 5);
    assert.equal(totalNeedsYou(null), 0);
  });

  // Feature: F-NEED-01
  it('reads a malformed response body as zero', () => {
    assert.deepEqual(parseNeedsYouCounts(null), { blocked: 0, plans_awaiting_decision: 0 });
    assert.deepEqual(parseNeedsYouCounts({ blocked: '4', plans_awaiting_decision: -1 }), {
      blocked: 0,
      plans_awaiting_decision: 0,
    });
    assert.deepEqual(parseNeedsYouCounts({ blocked: 4, plans_awaiting_decision: 1 }), {
      blocked: 4,
      plans_awaiting_decision: 1,
    });
  });

  // Feature: F-NEED-01
  it('links to blocked tasks, or to running tasks when only plans are waiting', () => {
    assert.equal(needsYouHref({ blocked: 1, plans_awaiting_decision: 2 }), '/tasks?status=blocked');
    assert.equal(needsYouHref({ blocked: 0, plans_awaiting_decision: 2 }), '/tasks?status=running');
    assert.equal(needsYouHref(null), '/tasks?status=blocked');
  });
});
