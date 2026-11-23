'use client';

/**
 * "Needs you" counts — one shared poll for the whole tab.
 *
 * Why it exists: several surfaces want the same two numbers (blocked tasks,
 * plans awaiting a decision). If each mounted its own `setInterval`, a page
 * with the sidebar plus a banner would hit `/api/needs-you` twice per tick,
 * forever, even in a background tab.
 *
 * Invariants:
 * - At most one timer and at most one request in flight per poll, however
 *   many subscribers there are.
 * - The first subscriber starts the poll (with an immediate fetch); the last
 *   unsubscribe stops it.
 * - Ticks are skipped while the document is hidden; nothing is fetched for a
 *   tab nobody is looking at.
 * - A failed fetch keeps the last known counts — a transient error must not
 *   flash the pill to zero.
 *
 * `createNeedsYouPoll` takes its fetcher, timer and visibility check as
 * arguments so the rules above are tested without a DOM or network.
 */

import { useEffect, useState } from 'react';
import { api } from './api-client';
import { onConnectivityRestored } from './connectivity-events';

export interface NeedsYouCounts {
  blocked: number;
  plans_awaiting_decision: number;
}

export const NEEDS_YOU_POLL_MS = 15_000;

/**
 * Where the pill links. TasksView filters by `?status=` (see `task-search.ts`).
 * Blocked tasks are the precise set; a plan awaiting a decision sits on a
 * `running` task, so when only plans are waiting the running list is the
 * closest filter (it may include other running tasks).
 */
export function needsYouHref(counts: NeedsYouCounts | null): string {
  if (counts && counts.blocked === 0 && counts.plans_awaiting_decision > 0) {
    return '/tasks?status=running';
  }
  return '/tasks?status=blocked';
}

export type NeedsYouListener = (counts: NeedsYouCounts) => void;

export interface NeedsYouPollDependencies {
  fetchCounts: () => Promise<NeedsYouCounts>;
  startTimer: (tick: () => void, intervalMs: number) => unknown;
  stopTimer: (handle: unknown) => void;
  isHidden: () => boolean;
  intervalMs: number;
}

export interface NeedsYouPoll {
  /** Register a listener; returns its unsubscribe. */
  subscribe: (listener: NeedsYouListener) => () => void;
  /** Fetch now (no-op while a fetch is already in flight). */
  refresh: () => Promise<void>;
  /** Last counts received, or null before the first successful fetch. */
  current: () => NeedsYouCounts | null;
}

export function totalNeedsYou(counts: NeedsYouCounts | null): number {
  if (!counts) return 0;
  return counts.blocked + counts.plans_awaiting_decision;
}

/** Coerce a response body to counts; anything malformed reads as zero. */
export function parseNeedsYouCounts(body: unknown): NeedsYouCounts {
  const record: Record<string, unknown> =
    body && typeof body === 'object' ? (body as Record<string, unknown>) : {};
  const count = (value: unknown) =>
    typeof value === 'number' && Number.isFinite(value) && value > 0 ? Math.floor(value) : 0;
  return {
    blocked: count(record.blocked),
    plans_awaiting_decision: count(record.plans_awaiting_decision),
  };
}

export function createNeedsYouPoll(dependencies: NeedsYouPollDependencies): NeedsYouPoll {
  const listeners = new Set<NeedsYouListener>();
  let timerHandle: unknown = null;
  let inFlight: Promise<void> | null = null;
  let latest: NeedsYouCounts | null = null;

  const refresh = (): Promise<void> => {
    if (inFlight) return inFlight;
    inFlight = (async () => {
      try {
        const counts = await dependencies.fetchCounts();
        latest = counts;
        for (const listener of [...listeners]) listener(counts);
      } catch {
        // Keep the last known counts; the next tick retries.
      } finally {
        inFlight = null;
      }
    })();
    return inFlight;
  };

  const tick = () => {
    if (dependencies.isHidden()) return;
    void refresh();
  };

  const subscribe = (listener: NeedsYouListener): (() => void) => {
    listeners.add(listener);
    if (latest) listener(latest);
    if (timerHandle === null) {
      timerHandle = dependencies.startTimer(tick, dependencies.intervalMs);
      tick();
    }
    let subscribed = true;
    return () => {
      if (!subscribed) return;
      subscribed = false;
      listeners.delete(listener);
      if (listeners.size === 0 && timerHandle !== null) {
        dependencies.stopTimer(timerHandle);
        timerHandle = null;
      }
    };
  };

  return { subscribe, refresh, current: () => latest };
}

/** The tab-wide poll every component shares. */
export const needsYouPoll: NeedsYouPoll = createNeedsYouPoll({
  fetchCounts: async () => parseNeedsYouCounts(await api.get<unknown>('/api/needs-you')),
  startTimer: (tick, intervalMs) => setInterval(tick, intervalMs),
  stopTimer: (handle) => clearInterval(handle as ReturnType<typeof setInterval>),
  isHidden: () => typeof document !== 'undefined' && document.hidden,
  intervalMs: NEEDS_YOU_POLL_MS,
});

/** Live counts from the shared poll; null until the first answer. */
export function useNeedsYou(poll: NeedsYouPoll = needsYouPoll): NeedsYouCounts | null {
  const [counts, setCounts] = useState<NeedsYouCounts | null>(() => poll.current());
  useEffect(() => poll.subscribe(setCounts), [poll]);
  // After an outage the counts are stale; don't make the operator wait a tick.
  useEffect(() => onConnectivityRestored(() => void poll.refresh()), [poll]);
  return counts;
}
