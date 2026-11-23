/**
 * Connectivity state machine for the dashboard's top banner.
 *
 * Why it exists: when the browser drops off the network, the Next.js server
 * stops, or the worker dies, every panel used to fail on its own and the
 * operator got a burst of "Network error" / "Bad gateway (502)" toasts with no
 * single explanation. This module decides, from a stream of inputs, which one
 * of four states the dashboard is in, so one banner can explain it once and
 * the per-request toasts it already explains can be suppressed.
 *
 * States: `online` | `offline` (the browser says so) | `server-down` (the
 * dashboard server did not answer, or answered the probe with an error) |
 * `worker-down` (the server answered but the worker did not).
 *
 * Invariants:
 * - `next()` is pure: same machine state + input → same result.
 * - `restored` is true exactly once per outage: on the transition from any
 *   non-online status back to `online`. Consecutive healthy probes while
 *   already online never re-fire it.
 * - Debounce (documented choice): leaving `online` because of a *probe*
 *   failure needs `FAILURE_THRESHOLD` (2) consecutive failures, so a single
 *   slow request or a server restart that completes within one poll does not
 *   flash a banner. A browser `offline` event is authoritative and applies
 *   immediately. Once degraded, a different failure kind switches status
 *   immediately (e.g. worker-down → server-down) — the operator is already
 *   looking at the banner and deserves the accurate reason.
 * - After the browser reports `online` again the status stays `offline`
 *   until a probe confirms the server is reachable; "the network is back"
 *   does not prove DevServer is.
 *
 * The module-level store at the bottom is the bridge between the banner
 * (which writes the current status) and `NotificationProvider` (which reads it
 * when deciding whether to toast). It is a plain variable, not React state,
 * because the fetch interceptor reads it outside the render cycle.
 */

export type ConnectivityStatus = 'online' | 'offline' | 'server-down' | 'worker-down';

/** Everything the machine reacts to. */
export type ConnectivityInput =
  | { kind: 'browser-offline' }
  | { kind: 'browser-online' }
  /** The probe answered 2xx with its body: the server is up by definition. */
  | { kind: 'probe-answered'; worker: boolean }
  /** The probe request itself rejected (refused, DNS, timeout, offline). */
  | { kind: 'probe-threw' }
  /** The probe got an HTTP answer that was not 2xx (e.g. a proxy's 502). */
  | { kind: 'probe-not-ok'; httpStatus: number };

export interface ConnectivityMachineState {
  status: ConnectivityStatus;
  /** Consecutive probe failures seen while still `online` (debounce). */
  failureStreak: number;
  /** Last thing the browser told us via `navigator.onLine` / events. */
  browserOnline: boolean;
}

export interface ConnectivityTransition {
  state: ConnectivityMachineState;
  /** True exactly on the non-online → online transition. */
  restored: boolean;
}

/** Consecutive probe failures needed before leaving `online`. */
export const FAILURE_THRESHOLD = 2;

/** Poll the probe this often while everything is healthy. */
export const HEALTHY_POLL_MS = 10_000;
/** Poll faster while degraded (or a failure is unconfirmed) to notice recovery sooner. */
export const DEGRADED_POLL_MS = 3_000;

/** The probe route; the fetch interceptor never toasts for it. */
export const CONNECTIVITY_PROBE_URL = '/api/health/connectivity';

export function initialConnectivityState(browserOnline = true): ConnectivityMachineState {
  return {
    status: browserOnline ? 'online' : 'offline',
    failureStreak: 0,
    browserOnline,
  };
}

function failureStatus(input: ConnectivityInput): ConnectivityStatus | null {
  switch (input.kind) {
    case 'probe-threw':
    case 'probe-not-ok':
      return 'server-down';
    case 'probe-answered':
      return input.worker ? null : 'worker-down';
    default:
      return null;
  }
}

/** Advance the machine by one input. Pure. */
export function next(
  current: ConnectivityMachineState,
  input: ConnectivityInput,
): ConnectivityTransition {
  if (input.kind === 'browser-offline') {
    return {
      state: { status: 'offline', failureStreak: 0, browserOnline: false },
      restored: false,
    };
  }

  if (input.kind === 'browser-online') {
    // Remember it, but wait for a probe before claiming DevServer is back.
    return { state: { ...current, browserOnline: true }, restored: false };
  }

  if (input.kind === 'probe-threw' && !current.browserOnline) {
    // The request failed because the browser is offline; that is already
    // what the banner says.
    return {
      state: { status: 'offline', failureStreak: 0, browserOnline: false },
      restored: false,
    };
  }

  const failed = failureStatus(input);
  if (failed === null) {
    // A 2xx probe with a healthy worker: fully online.
    return {
      state: { status: 'online', failureStreak: 0, browserOnline: true },
      restored: current.status !== 'online',
    };
  }

  // A probe that answered at all proves the browser has a network.
  const browserOnline = input.kind === 'probe-answered' ? true : current.browserOnline;

  if (current.status === 'online') {
    const failureStreak = current.failureStreak + 1;
    if (failureStreak < FAILURE_THRESHOLD) {
      return { state: { status: 'online', failureStreak, browserOnline }, restored: false };
    }
    return { state: { status: failed, failureStreak, browserOnline }, restored: false };
  }

  return {
    state: { status: failed, failureStreak: current.failureStreak + 1, browserOnline },
    restored: false,
  };
}

/**
 * How long to wait before the next probe. Fast while degraded or while a
 * failure awaits confirmation, so both the banner and the "restored" flash
 * appear within a few seconds of reality.
 */
export function pollIntervalMs(state: ConnectivityMachineState): number {
  return state.status === 'online' && state.failureStreak === 0 ? HEALTHY_POLL_MS : DEGRADED_POLL_MS;
}

/** Plain-language banner text; null for `online` (no banner). */
export function bannerMessage(status: ConnectivityStatus): string | null {
  switch (status) {
    case 'offline':
      return 'You are offline. DevServer will reconnect when your network is back.';
    case 'server-down':
      return 'The DevServer dashboard server is not responding. Retrying…';
    case 'worker-down':
      return 'The DevServer worker is not responding — tasks cannot start or report progress. Retrying…';
    default:
      return null;
  }
}

export interface FailedRequest {
  /** HTTP status; 0 (or anything) when `isNetworkError` is true. */
  status: number;
  /** The fetch itself rejected: no HTTP answer at all. */
  isNetworkError: boolean;
}

/**
 * Should the per-request error toast be dropped because the banner already
 * explains it?
 *
 * - `online`: never suppress — the banner is silent, so the toast is the
 *   only signal.
 * - Any degraded status explains network failures (the request could not
 *   reach the server, or the server is gone).
 * - `worker-down` explains 502 (`proxyWorker`'s "Worker unreachable").
 * - `offline` / `server-down` explain the gateway family 502/503/504 that a
 *   reverse proxy answers while the server is away.
 *
 * A 503 carrying a DB diagnostic is not routed here at all — the provider
 * shows that sticky toast unconditionally, because the banner does not cover
 * a database outage.
 */
export function shouldSuppressToast(status: ConnectivityStatus, failure: FailedRequest): boolean {
  if (status === 'online') return false;
  if (failure.isNetworkError) return true;
  if (status === 'worker-down') return failure.status === 502;
  return failure.status === 502 || failure.status === 503 || failure.status === 504;
}

/** True for the probe URL in any form the interceptor may see. */
export function isConnectivityProbeUrl(url: string): boolean {
  try {
    return new URL(url, 'http://local.invalid').pathname === CONNECTIVITY_PROBE_URL;
  } catch {
    return false;
  }
}

// ─── Shared current-status store ────────────────────────────────────────────

type StatusListener = (status: ConnectivityStatus) => void;

let currentStatus: ConnectivityStatus = 'online';
const statusListeners = new Set<StatusListener>();

/** The status the banner last published; `online` until it mounts. */
export function getConnectivityStatus(): ConnectivityStatus {
  return currentStatus;
}

/** Called by the banner; notifies subscribers only on change. */
export function setConnectivityStatus(status: ConnectivityStatus): void {
  if (status === currentStatus) return;
  currentStatus = status;
  for (const listener of [...statusListeners]) {
    try {
      listener(status);
    } catch {
      // A subscriber's failure must not stop the others from updating.
    }
  }
}

/** Subscribe to status changes; returns the unsubscribe function. */
export function subscribeConnectivityStatus(listener: StatusListener): () => void {
  statusListeners.add(listener);
  return () => {
    statusListeners.delete(listener);
  };
}
