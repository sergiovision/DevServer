'use client';

import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
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
 *
 * It also owns whether the Ask Agent panel is open, so surfaces other than
 * the floating button (⌘J, the command palette's "Ask Agent: <query>" row)
 * can open it. `openAskAgent(prompt)` parks the prompt; the panel takes it
 * once with `takePendingPrompt()` when it mounts. Both additions are optional
 * to consumers — nothing that ignores them changes behaviour.
 */

interface AssistantContextValue {
  page: PageContext;
  /** Repo the panel should scope tools to, when the page implies one. */
  repoId: number | null;
  setEntity: (entity: PageContext['entity'] | null, title?: string) => void;
  setRepoId: (id: number | null) => void;
  /** Whether the Ask Agent drawer is open. */
  askAgentOpen: boolean;
  setAskAgentOpen: (open: boolean | ((previous: boolean) => boolean)) => void;
  /** Open the drawer, optionally with a question to prefill. */
  openAskAgent: (prompt?: string) => void;
  /** Return and clear the prompt passed to `openAskAgent` (once). */
  takePendingPrompt: () => string | null;
  /**
   * Bumped whenever `openAskAgent` parks a prompt, so an already-open panel
   * can pick it up: `useEffect(() => { … takePendingPrompt() … }, [version])`.
   */
  pendingPromptVersion: number;
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
  const [askAgentOpen, setAskAgentOpen] = useState(false);
  // A ref, not state: the prompt is a one-shot handoff and must not re-render
  // (or re-fill) the panel after it has been consumed.
  const pendingPromptRef = useRef<string | null>(null);
  const [pendingPromptVersion, setPendingPromptVersion] = useState(0);

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

  const openAskAgent = useCallback((prompt?: string) => {
    const trimmed = prompt?.trim();
    pendingPromptRef.current = trimmed ? trimmed : null;
    if (trimmed) setPendingPromptVersion((version) => version + 1);
    setAskAgentOpen(true);
  }, []);

  const takePendingPrompt = useCallback(() => {
    const prompt = pendingPromptRef.current;
    pendingPromptRef.current = null;
    return prompt;
  }, []);

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
      askAgentOpen,
      setAskAgentOpen,
      openAskAgent,
      takePendingPrompt,
      pendingPromptVersion,
    }),
    [
      pathname,
      title,
      entity,
      repoId,
      setEntity,
      askAgentOpen,
      openAskAgent,
      takePendingPrompt,
      pendingPromptVersion,
    ],
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
    askAgentOpen: false,
    setAskAgentOpen: () => {},
    openAskAgent: () => {},
    takePendingPrompt: () => null,
    pendingPromptVersion: 0,
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
