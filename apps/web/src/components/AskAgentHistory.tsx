'use client';

/**
 * Recent Ask Agent conversations — a compact list inside the drawer.
 *
 * A thin view over `lib/ask-agent-history.ts`: it renders what it is given and
 * reports open / rename / delete / new back to the panel, which owns the
 * store. Nothing here touches storage or the network, so it keeps working when
 * Postgres (or the worker) is down — history is browser-local by design.
 */

import React, { useState } from 'react';
import { CButton, CFormInput } from '@coreui/react-pro';
import CIcon from '@coreui/icons-react';
import { cilPencil, cilPlus, cilTrash } from '@coreui/icons';
import type { StoredConversation } from '@/lib/ask-agent-history';

interface AskAgentHistoryProps {
  conversations: readonly StoredConversation[];
  activeId: string;
  onOpen: (id: string) => void;
  onRename: (id: string, title: string) => void;
  onDelete: (id: string) => void;
  onNew: () => void;
}

function relativeTime(timestamp: number, now: number): string {
  const minutes = Math.round((now - timestamp) / 60_000);
  if (minutes < 1) return 'just now';
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return new Date(timestamp).toLocaleDateString();
}

export function AskAgentHistory({ conversations, activeId, onOpen, onRename, onDelete, onNew }: AskAgentHistoryProps) {
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [draftTitle, setDraftTitle] = useState('');
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);
  const now = Date.now();

  const commitRename = () => {
    if (renamingId) onRename(renamingId, draftTitle);
    setRenamingId(null);
  };

  return (
    <div className="d-flex flex-column gap-1">
      <div className="d-flex align-items-center mb-1">
        <small className="text-body-secondary fw-semibold">Recent conversations</small>
        <CButton size="sm" color="primary" variant="ghost" className="ms-auto" onClick={onNew}>
          <CIcon icon={cilPlus} size="sm" className="me-1" />
          New
        </CButton>
      </div>

      {conversations.length === 0 && (
        <small className="text-body-tertiary">No saved conversations yet. They are kept in this browser only.</small>
      )}

      {conversations.map((conversation) => {
        const active = conversation.id === activeId;
        if (renamingId === conversation.id) {
          return (
            <CFormInput
              key={conversation.id}
              size="sm"
              autoFocus
              value={draftTitle}
              aria-label="Conversation title"
              onChange={(event) => setDraftTitle(event.target.value)}
              onBlur={commitRename}
              onKeyDown={(event) => {
                if (event.key === 'Enter') {
                  event.preventDefault();
                  commitRename();
                } else if (event.key === 'Escape') {
                  // Cancel the rename only — do not let the drawer's Esc close it.
                  event.preventDefault();
                  event.stopPropagation();
                  setRenamingId(null);
                }
              }}
            />
          );
        }
        return (
          <div
            key={conversation.id}
            className="d-flex align-items-center gap-1 rounded px-2 py-1"
            style={{
              background: active ? 'var(--cui-primary-bg-subtle)' : 'var(--cui-tertiary-bg)',
              border: '1px solid var(--cui-border-color)',
            }}
          >
            <button
              type="button"
              className="btn btn-link p-0 text-start text-decoration-none flex-grow-1 text-truncate"
              style={{ color: 'var(--cui-body-color)', minWidth: 0 }}
              onClick={() => onOpen(conversation.id)}
              title={conversation.title}
            >
              <span className="small d-block text-truncate">{conversation.title}</span>
              <span className="text-body-tertiary" style={{ fontSize: '0.72rem' }}>
                {relativeTime(conversation.updatedAt, now)} · {conversation.messages.length} messages
              </span>
            </button>
            {confirmDeleteId === conversation.id ? (
              <>
                <CButton
                  size="sm"
                  color="danger"
                  variant="ghost"
                  onClick={() => {
                    setConfirmDeleteId(null);
                    onDelete(conversation.id);
                  }}
                >
                  Delete
                </CButton>
                <CButton size="sm" color="secondary" variant="ghost" onClick={() => setConfirmDeleteId(null)}>
                  Keep
                </CButton>
              </>
            ) : (
              <>
                <CButton
                  size="sm"
                  color="secondary"
                  variant="ghost"
                  title="Rename"
                  aria-label="Rename conversation"
                  onClick={() => {
                    setDraftTitle(conversation.title);
                    setRenamingId(conversation.id);
                  }}
                >
                  <CIcon icon={cilPencil} size="sm" />
                </CButton>
                <CButton
                  size="sm"
                  color="secondary"
                  variant="ghost"
                  title="Delete"
                  aria-label="Delete conversation"
                  onClick={() => setConfirmDeleteId(conversation.id)}
                >
                  <CIcon icon={cilTrash} size="sm" />
                </CButton>
              </>
            )}
          </div>
        );
      })}
    </div>
  );
}
