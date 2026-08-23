'use client';

import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from 'react';
import { usePathname } from 'next/navigation';
import type { PageContext } from '@/lib/assistant-client';

/**
 * Tells the Ask Agent panel where the operator is.
 *
 * Two levels, so no page is obliged to do anything:
 *
 * - **Automatic** — the pathname alone, normalised to a route key
 *   (`/tasks/42` → `/tasks/[id]`). Every page gets this for free, and the
 *   worker's handbook manifest turns the route into a screen description.
 * - **Registered** — a page that knows more calls `useAssistantPageContext`
 *   with the entity on screen (task key and status, repo name, …), so
 *   "why is this stuck?" can be answered about *that* task.
 */

interface AssistantContextValue {
  page: PageContext;
  /** Repo the panel should scope tools to, when the page implies one. */
  repoId: number | null;
  setEntity: (entity: PageContext['entity'] | null, title?: string) => void;
  setRepoId: (id: number | null) => void;
}

const AssistantCtx = createContext<AssistantContextValue | null>(null);

/** `/tasks/42` → `/tasks/[id]`. Mirrors normalize_route() in assistant_kb.py. */
export function normalizeRoute(pathname: string | null): string {
  if (!pathname) return '/';
  const route = pathname.replace(/\/\d+(?=\/|$)/g, '/[id]').replace(/\/+$/, '');
  return route || '/';
}

export function AssistantProvider({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const [entity, setEntityState] = useState<PageContext['entity'] | null>(null);
  const [title, setTitle] = useState<string | undefined>(undefined);
  const [repoId, setRepoId] = useState<number | null>(null);

  // Entity context belongs to one screen. Clearing it on navigation prevents
  // the panel from confidently answering about the task you just left.
  useEffect(() => {
    setEntityState(null);
    setTitle(undefined);
    setRepoId(null);
  }, [pathname]);

  const setEntity = useCallback(
    (next: PageContext['entity'] | null, nextTitle?: string) => {
      setEntityState(next);
      if (nextTitle !== undefined) setTitle(nextTitle);
    },
    [],
  );

  const value = useMemo<AssistantContextValue>(
    () => ({
      page: {
        path: pathname || '/',
        route: normalizeRoute(pathname),
        ...(title ? { title } : {}),
        ...(entity ? { entity } : {}),
      },
      repoId,
      setEntity,
      setRepoId,
    }),
    [pathname, title, entity, repoId, setEntity],
  );

  return <AssistantCtx.Provider value={value}>{children}</AssistantCtx.Provider>;
}

/** Read the current page context (used by the panel itself). */
export function useAssistantContext(): AssistantContextValue {
  const ctx = useContext(AssistantCtx);
  if (ctx) return ctx;
  // The provider is mounted in the root layout, so this only happens in
  // isolated renders (tests, storybook). Degrade rather than throw — a missing
  // context must never break a page.
  return {
    page: { path: '/', route: '/' },
    repoId: null,
    setEntity: () => {},
    setRepoId: () => {},
  };
}

/**
 * Register what this screen is about, so the assistant can answer about it.
 *
 * ```tsx
 * useAssistantPageContext(
 *   { kind: 'task', id: task.id, key: task.task_key, status: task.status },
 *   'Task detail',
 *   task.repo_id,
 * );
 * ```
 *
 * Safe to call with `null` while data is loading.
 */
export function useAssistantPageContext(
  entity: PageContext['entity'] | null,
  title?: string,
  repoId?: number | null,
): void {
  const { setEntity, setRepoId } = useAssistantContext();
  // Serialised so a fresh object identity on every render does not re-fire.
  const key = JSON.stringify({ entity, title });
  useEffect(() => {
    setEntity(entity, title);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  useEffect(() => {
    if (repoId !== undefined) setRepoId(repoId ?? null);
  }, [repoId, setRepoId]);
}
