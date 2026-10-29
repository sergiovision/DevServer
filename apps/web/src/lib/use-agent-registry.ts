'use client';

/**
 * Client-side access to the agent registry (vendors + models).
 *
 * Several pickers can be on one page (primary model, backup model, settings),
 * so the request is shared: the first component to mount starts the fetch and
 * every other one awaits the same promise. A failed load is forgotten, so the
 * next mount retries instead of caching the error for the whole session.
 */

import { useEffect, useState } from 'react';
import type { AgentRegistry } from './agent-vendors';

let sharedRequest: Promise<AgentRegistry | null> | null = null;
let loadedRegistry: AgentRegistry | null = null;

async function requestRegistry(): Promise<AgentRegistry | null> {
  try {
    const res = await fetch('/api/agent-registry');
    if (!res.ok) return null;
    return (await res.json()) as AgentRegistry;
  } catch {
    return null;
  }
}

function loadRegistryOnce(): Promise<AgentRegistry | null> {
  if (!sharedRequest) {
    sharedRequest = requestRegistry().then((registry) => {
      if (registry) {
        loadedRegistry = registry;
      } else {
        sharedRequest = null; // allow a retry on the next mount
      }
      return registry;
    });
  }
  return sharedRequest;
}

/**
 * The agent registry, or `null` while it is loading (or if the worker is
 * unreachable). All helpers in `agent-vendors.ts` accept `null`, so callers
 * can pass the value straight through without branching.
 */
export function useAgentRegistry(): AgentRegistry | null {
  const [registry, setRegistry] = useState<AgentRegistry | null>(loadedRegistry);

  useEffect(() => {
    if (registry) return;
    let unmounted = false;
    loadRegistryOnce().then((loaded) => {
      if (!unmounted && loaded) setRegistry(loaded);
    });
    return () => {
      unmounted = true;
    };
  }, [registry]);

  return registry;
}
