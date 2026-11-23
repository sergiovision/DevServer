'use client';

/**
 * React hooks for the two shapes of async work components do.
 *
 * Why it exists: every panel re-implemented `loading` / `error` state,
 * forgot to ignore responses that arrived after a newer request, and set
 * state after unmount. These hooks hold that bookkeeping once:
 *
 * - `useAsyncAction` — a user-triggered mutation (save, cancel, run): a busy
 *   flag, the last error, and no `setState` after unmount.
 * - `useResource` — data a panel shows: load / reload, stale responses
 *   dropped (only the newest request may write state), an optional poll that
 *   pauses while the tab is hidden, and an automatic reload when connectivity
 *   is restored (see `connectivity.ts`).
 *
 * The stale-response rule lives in `createLatestRequestGate`, a pure helper
 * tested without a DOM.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { onConnectivityRestored } from './connectivity-events';

/**
 * Hands out monotonically increasing tickets; only the newest ticket is
 * "current". A response whose ticket is no longer current is stale and must
 * not overwrite newer data.
 */
export interface LatestRequestGate {
  next(): number;
  isCurrent(ticket: number): boolean;
  /** Invalidate every outstanding ticket (unmount). */
  close(): void;
}

export function createLatestRequestGate(): LatestRequestGate {
  let latest = 0;
  let closed = false;
  return {
    next: () => ++latest,
    isCurrent: (ticket) => !closed && ticket === latest,
    close: () => {
      closed = true;
    },
  };
}

function isAbort(err: unknown): boolean {
  return err instanceof DOMException && err.name === 'AbortError';
}

export interface AsyncAction<A extends unknown[], R> {
  run: (...args: A) => Promise<R | undefined>;
  busy: boolean;
  error: unknown;
  clearError: () => void;
}

/**
 * Wrap a mutation. `run` resolves to the result, or `undefined` when it threw
 * (the error is kept in `error`); it never rejects, so click handlers can
 * `void run()` safely.
 */
export function useAsyncAction<A extends unknown[], R>(
  action: (...args: A) => Promise<R>,
): AsyncAction<A, R> {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const mounted = useRef(true);
  const actionRef = useRef(action);
  actionRef.current = action;

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const run = useCallback(async (...args: A): Promise<R | undefined> => {
    setBusy(true);
    setError(null);
    try {
      return await actionRef.current(...args);
    } catch (err) {
      if (mounted.current) setError(err);
      return undefined;
    } finally {
      if (mounted.current) setBusy(false);
    }
  }, []);

  const clearError = useCallback(() => setError(null), []);
  return { run, busy, error, clearError };
}

export interface ResourceOptions {
  /** Poll interval in ms; polling pauses while `document.hidden`. */
  pollMs?: number;
  /** Skip loading while false (e.g. an id is not known yet). */
  enabled?: boolean;
  /** Reload when the connectivity banner reports "restored" (default true). */
  reloadOnRestore?: boolean;
}

export interface Resource<T> {
  data: T | undefined;
  error: unknown;
  loading: boolean;
  reload: () => Promise<void>;
  /** Replace the data locally (optimistic update) without a request. */
  mutate: (next: T | ((prev: T | undefined) => T)) => void;
}

/**
 * Load data for display. `loader` receives an `AbortSignal` that fires when a
 * newer load supersedes it or the component unmounts. `deps` re-trigger the
 * load the same way `useEffect` deps do.
 */
export function useResource<T>(
  loader: (signal: AbortSignal) => Promise<T>,
  deps: ReadonlyArray<unknown>,
  options: ResourceOptions = {},
): Resource<T> {
  const { pollMs, enabled = true, reloadOnRestore = true } = options;
  const [data, setData] = useState<T | undefined>(undefined);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState<boolean>(enabled);
  const loaderRef = useRef(loader);
  loaderRef.current = loader;
  const gateRef = useRef<LatestRequestGate>(createLatestRequestGate());
  const controllerRef = useRef<AbortController | null>(null);

  const load = useCallback(async () => {
    const gate = gateRef.current;
    const ticket = gate.next();
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    setLoading(true);
    try {
      const result = await loaderRef.current(controller.signal);
      if (!gate.isCurrent(ticket)) return;
      setData(result);
      setError(null);
    } catch (err) {
      if (!gate.isCurrent(ticket) || isAbort(err)) return;
      setError(err);
    } finally {
      if (gate.isCurrent(ticket)) setLoading(false);
    }
  }, []);

  useEffect(() => {
    // A fresh gate per dependency set: responses for old deps are stale.
    gateRef.current = createLatestRequestGate();
    if (!enabled) {
      setLoading(false);
      return;
    }
    void load();
    const gate = gateRef.current;
    return () => {
      gate.close();
      controllerRef.current?.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, load, ...deps]);

  useEffect(() => {
    if (!enabled || !pollMs) return;
    const timer = setInterval(() => {
      if (typeof document !== 'undefined' && document.hidden) return;
      void load();
    }, pollMs);
    return () => clearInterval(timer);
  }, [enabled, pollMs, load]);

  useEffect(() => {
    if (!enabled || !reloadOnRestore) return;
    return onConnectivityRestored(() => void load());
  }, [enabled, reloadOnRestore, load]);

  const mutate = useCallback((next: T | ((prev: T | undefined) => T)) => {
    setData((prev) => (typeof next === 'function' ? (next as (p: T | undefined) => T)(prev) : next));
  }, []);

  return { data, error, loading, reload: load, mutate };
}
