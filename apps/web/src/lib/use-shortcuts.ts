'use client';

/**
 * React binding for the global shortcut table in `shortcuts.ts`.
 *
 * Each caller passes handlers only for the actions it owns (the palette owns
 * ⌘K and `?`, the Ask Agent launcher owns ⌘J); an action with no handler in
 * this hook is left alone, so the browser default and other listeners still
 * see the key. Matching and the "not while typing" rule live in the pure
 * module — this file only adapts a real `KeyboardEvent`.
 *
 * Handlers are read through a ref, so callers can pass a fresh object every
 * render without re-binding the window listener.
 */

import { useEffect, useRef } from 'react';
import { matchShortcut, type ShortcutActionId } from './shortcuts';

export type ShortcutHandlers = Partial<Record<ShortcutActionId, () => void>>;

export function useShortcuts(handlers: ShortcutHandlers): void {
  const handlersRef = useRef(handlers);
  handlersRef.current = handlers;

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const action = matchShortcut({
        key: event.key,
        metaKey: event.metaKey,
        ctrlKey: event.ctrlKey,
        altKey: event.altKey,
        shiftKey: event.shiftKey,
        isComposing: event.isComposing,
        target: event.target instanceof HTMLElement ? event.target : null,
      });
      if (!action) return;
      const handler = handlersRef.current[action];
      if (!handler) return;
      event.preventDefault();
      handler();
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, []);
}
