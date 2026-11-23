'use client';

/**
 * ⌘/Ctrl+K command palette: jump to any page, find a task by key or title, or
 * hand the query to Ask Agent (always the last row). Also owns the other
 * global shortcuts that are not Ask Agent's — `?` (shortcut list) and `N`
 * (new task) — so every binding in `lib/shortcuts.ts` has exactly one handler.
 *
 * Built on CoreUI `CModal` rather than a palette library (cmdk is not
 * installed): no extra dependency, it inherits the existing CoreUI theme and
 * dark mode, and it brings a focus trap and Escape-to-close for free. Ranking
 * is the pure `lib/command-palette.ts`; this component only renders and routes.
 *
 * Mount once, inside AssistantProvider (AppShell does both).
 */

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { usePathname, useRouter } from 'next/navigation';
import { CBadge, CModal, CModalBody } from '@coreui/react-pro';
import CIcon from '@coreui/icons-react';
import { cilCommentSquare, cilSearch, cilTask } from '@coreui/icons';
import { api } from '@/lib/api-client';
import { useResource } from '@/lib/use-async';
import { useShortcuts } from '@/lib/use-shortcuts';
import { NAV_ITEMS } from '@/lib/nav-items';
import {
  buildPaletteResults,
  moveSelection,
  type PaletteEntry,
  type PaletteTask,
} from '@/lib/command-palette';
import { useAssistantContext } from './AssistantProvider';
import { ShortcutHelp } from './ShortcutHelp';

/**
 * `/api/tasks` defaults to 100 rows ordered by priority then recency; ask for
 * more so older tasks are still findable by key. Filtering is client-side.
 */
const TASK_SEARCH_URL = '/api/tasks?limit=500';

/**
 * The modal's focus trap restores focus to its previously focused element as
 * its close transition starts (CoreUI default 150ms). Opening Ask Agent after
 * that keeps its textarea focused instead of losing focus to the dying modal.
 */
const ASK_AGENT_HANDOFF_MS = 200;

const STATUS_COLOR: Record<string, string> = {
  running: 'primary',
  verifying: 'info',
  blocked: 'warning',
  failed: 'danger',
  test: 'success',
  done: 'success',
};

function entryIcon(entry: PaletteEntry) {
  if (entry.kind === 'ask-agent') return cilCommentSquare;
  if (entry.kind === 'task') return cilTask;
  return NAV_ITEMS.find((item) => item.href === entry.href)?.icon ?? cilSearch;
}

export function CommandPalette() {
  const router = useRouter();
  const pathname = usePathname();
  const { openAskAgent } = useAssistantContext();
  const [open, setOpen] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [selected, setSelected] = useState(0);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const listRef = useRef<HTMLUListElement | null>(null);

  const tasks = useResource<PaletteTask[]>(
    (signal) => api.get<PaletteTask[]>(TASK_SEARCH_URL, { signal }),
    [open],
    { enabled: open, reloadOnRestore: false },
  );

  const results = useMemo(
    () =>
      buildPaletteResults({
        query,
        pages: NAV_ITEMS,
        tasks: Array.isArray(tasks.data) ? tasks.data : [],
      }),
    [query, tasks.data],
  );

  const disabled = pathname === '/setup';

  useShortcuts(
    disabled
      ? {}
      : {
          'open-command-palette': () => {
            setHelpOpen(false);
            setOpen((value) => !value);
          },
          'show-shortcuts': () => setHelpOpen((value) => !value),
          'new-task': () => router.push('/tasks/new'),
        },
  );

  // A fresh palette every time: stale text from the last search is noise.
  useEffect(() => {
    if (!open) return;
    setQuery('');
    setSelected(0);
  }, [open]);

  useEffect(() => {
    setSelected(0);
  }, [query]);

  // Keep the highlighted row in view while arrowing through a long list.
  useEffect(() => {
    listRef.current
      ?.querySelector<HTMLElement>(`[data-index="${selected}"]`)
      ?.scrollIntoView({ block: 'nearest' });
  }, [selected]);

  const close = useCallback(() => setOpen(false), []);

  const activate = useCallback(
    (entry: PaletteEntry | undefined) => {
      if (!entry) return;
      setOpen(false);
      if (entry.kind === 'ask-agent') {
        window.setTimeout(() => openAskAgent(entry.query), ASK_AGENT_HANDOFF_MS);
        return;
      }
      router.push(entry.href);
    },
    [openAskAgent, router],
  );

  const onInputKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      setSelected((current) =>
        moveSelection(current, event.key === 'ArrowDown' ? 1 : -1, results.length),
      );
    } else if (event.key === 'Enter' && !event.nativeEvent.isComposing) {
      event.preventDefault();
      activate(results[selected]);
    }
  };

  if (disabled) return null;

  return (
    <>
      <CModal
        visible={open}
        onClose={close}
        onShow={() => inputRef.current?.focus()}
        alignment="top"
        size="lg"
        aria-label="Command palette"
        className="command-palette"
      >
        <CModalBody className="p-0">
          <div className="d-flex align-items-center gap-2 px-3 py-2 border-bottom">
            <CIcon icon={cilSearch} className="text-body-secondary" />
            <input
              ref={inputRef}
              autoFocus
              type="text"
              className="form-control border-0 shadow-none px-0"
              placeholder="Go to a page, find a task, or ask the agent…"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              onKeyDown={onInputKeyDown}
              role="combobox"
              aria-expanded="true"
              aria-controls="command-palette-results"
              aria-activedescendant={results[selected] ? `command-palette-${selected}` : undefined}
              aria-autocomplete="list"
            />
            <kbd className="small text-nowrap">Esc</kbd>
          </div>
          <ul
            ref={listRef}
            id="command-palette-results"
            role="listbox"
            className="list-unstyled mb-0 py-1 overflow-auto"
            style={{ maxHeight: '60vh' }}
          >
            {results.map((entry, index) => (
              <li
                key={entry.id}
                id={`command-palette-${index}`}
                data-index={index}
                role="option"
                aria-selected={index === selected}
                className={`d-flex align-items-center gap-2 px-3 py-2 ${
                  index === selected ? 'bg-body-secondary' : ''
                }`}
                style={{ cursor: 'pointer' }}
                onMouseMove={() => setSelected(index)}
                // mousedown, not click: keeps focus in the input until we act.
                onMouseDown={(event) => {
                  event.preventDefault();
                  activate(entry);
                }}
              >
                <CIcon icon={entryIcon(entry)} className="text-body-secondary flex-shrink-0" />
                <span className="text-truncate flex-grow-1">{entry.label}</span>
                {entry.kind === 'task' && entry.status && (
                  <CBadge color={STATUS_COLOR[entry.status] ?? 'secondary'}>{entry.status}</CBadge>
                )}
                {entry.kind === 'page' && <small className="text-body-secondary">Page</small>}
              </li>
            ))}
          </ul>
          <div className="px-3 py-2 border-top small text-body-secondary d-flex gap-3">
            <span>
              <kbd>↑</kbd> <kbd>↓</kbd> to move
            </span>
            <span>
              <kbd>Enter</kbd> to open
            </span>
            <span>
              <kbd>?</kbd> all shortcuts
            </span>
            {tasks.loading && <span className="ms-auto">Loading tasks…</span>}
          </div>
        </CModalBody>
      </CModal>
      <ShortcutHelp visible={helpOpen} onClose={() => setHelpOpen(false)} />
    </>
  );
}
