'use client';

import React from 'react';
import { usePathname } from 'next/navigation';
import CIcon from '@coreui/icons-react';
import { cilCommentSquare } from '@coreui/icons';
import { AskAgentPanel } from './AskAgentPanel';
import { useAssistantContext } from './AssistantProvider';
import { useShortcuts } from '@/lib/use-shortcuts';

/**
 * The floating "Ask Agent" button and the drawer it opens.
 *
 * Mounted once in AppShell so it is present on every dashboard page — and
 * therefore inherits AppShell's `/setup` early return, which keeps the wizard
 * chrome-less without a second condition here.
 *
 * The panel is mounted lazily on first open: its bundle, and its capabilities
 * request, cost nothing to operators who never click.
 *
 * Open state lives in AssistantProvider so the command palette can open the
 * panel too. ⌘/Ctrl+J toggles it; ⌘/Ctrl+K now belongs to the command palette
 * (which offers "Ask Agent: <query>" as its last row). Escape closing is
 * handled by the panel itself and is unchanged.
 */
export function AskAgentLauncher() {
  const { askAgentOpen: open, setAskAgentOpen: setOpen } = useAssistantContext();
  const pathname = usePathname();

  useShortcuts({ 'open-ask-agent': () => setOpen((value) => !value) });

  if (pathname === '/setup') return null;

  return (
    <>
      {open && <AskAgentPanel onClose={() => setOpen(false)} />}
      <button
        type="button"
        className={`ask-agent-fab btn btn-primary rounded-circle shadow ${open ? 'is-open' : ''}`}
        onClick={() => setOpen((value) => !value)}
        title={open ? 'Close Ask Agent (Esc)' : 'Ask Agent  (⌘/Ctrl + J)'}
        aria-label="Ask Agent"
        aria-keyshortcuts="Meta+J Control+J"
        aria-expanded={open}
      >
        <CIcon icon={cilCommentSquare} size="lg" />
      </button>
    </>
  );
}
