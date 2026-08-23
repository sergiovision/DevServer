'use client';

import React, { useEffect, useState } from 'react';
import { usePathname } from 'next/navigation';
import CIcon from '@coreui/icons-react';
import { cilCommentSquare } from '@coreui/icons';
import { AskAgentPanel } from './AskAgentPanel';

/**
 * The floating "Ask Agent" button and the drawer it opens.
 *
 * Mounted once in AppShell so it is present on every dashboard page — and
 * therefore inherits AppShell's `/setup` early return, which keeps the wizard
 * chrome-less without a second condition here.
 *
 * The panel is mounted lazily on first open: its bundle, and its capabilities
 * request, cost nothing to operators who never click.
 */
export function AskAgentLauncher() {
  const [open, setOpen] = useState(false);
  const pathname = usePathname();

  // ⌘/Ctrl + K is the near-universal "ask something" shortcut.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        setOpen((v) => !v);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  if (pathname === '/setup') return null;

  return (
    <>
      {open && <AskAgentPanel onClose={() => setOpen(false)} />}
      <button
        type="button"
        className={`ask-agent-fab btn btn-primary rounded-circle shadow ${open ? 'is-open' : ''}`}
        onClick={() => setOpen((v) => !v)}
        title={open ? 'Close Ask Agent (Esc)' : 'Ask Agent  (⌘/Ctrl + K)'}
        aria-label="Ask Agent"
        aria-expanded={open}
      >
        <CIcon icon={cilCommentSquare} size="lg" />
      </button>
    </>
  );
}
