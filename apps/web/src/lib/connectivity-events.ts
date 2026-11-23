/**
 * In-page "connectivity restored" signal.
 *
 * Why it exists: when the server or worker comes back, every panel that
 * showed an error should reload on its own instead of waiting for the
 * operator to refresh. The connectivity banner owns the state machine
 * (`connectivity.ts`) and calls `emitConnectivityRestored()` exactly once per
 * outage; `useResource` subscribes. Kept separate from the state machine so
 * the hook does not depend on the banner component being mounted.
 */

type Listener = () => void;

const listeners = new Set<Listener>();

/** Subscribe; returns the unsubscribe function (fits `useEffect` cleanup). */
export function onConnectivityRestored(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function emitConnectivityRestored(): void {
  for (const listener of [...listeners]) {
    try {
      listener();
    } catch {
      // One panel's reload failure must not stop the others from reloading.
    }
  }
}

/** Test hook: how many panels are currently listening. */
export function connectivityListenerCount(): number {
  return listeners.size;
}
