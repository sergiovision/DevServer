'use client';

import { useCallback, useEffect, useState } from 'react';
import {
  CAlert,
  CBadge,
  CButton,
  CCard,
  CCardBody,
  CCardHeader,
  CForm,
  CFormTextarea,
} from '@coreui/react-pro';

interface TaskNote {
  id: number;
  body: string;
  created_at: string;
  delivered_at: string | null;
}

interface TaskNotesPanelProps {
  taskId: number;
}

/**
 * Notes from the operator to the task's agent.
 *
 * A note is not a chat message: the agent does not answer it. The worker
 * folds undelivered notes into the prompt of the task's next attempt and
 * stamps them delivered. A note left on a finished task is stored for the
 * record only — nothing will ever read it.
 */
export function TaskNotesPanel({ taskId }: TaskNotesPanelProps) {
  const [notes, setNotes] = useState<TaskNote[]>([]);
  const [draft, setDraft] = useState('');
  const [posting, setPosting] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  const loadNotes = useCallback(async () => {
    try {
      const res = await fetch(`/api/tasks/${taskId}/notes`);
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || 'Could not load notes');
      setNotes(Array.isArray(data.notes) ? data.notes : []);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load notes');
    }
  }, [taskId]);

  useEffect(() => {
    loadNotes();
  }, [loadNotes]);

  const postNote = async () => {
    if (!draft.trim()) return;
    setPosting(true);
    setError('');
    setNotice('');
    try {
      const res = await fetch(`/api/tasks/${taskId}/notes`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ body: draft }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || 'Could not save the note');
      setDraft('');
      setNotice(
        data.will_be_delivered
          ? 'Saved. The agent will read it at the start of its next attempt.'
          : 'Saved for the record. This task is not running, so the agent will not read it.',
      );
      await loadNotes();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not save the note');
    } finally {
      setPosting(false);
    }
  };

  return (
    <CCard className="mb-4">
      <CCardHeader><strong>Notes for the Agent</strong></CCardHeader>
      <CCardBody>
        <p className="small text-body-secondary">
          Notes reach the agent at its next attempt. Notes on a finished task are
          kept for the record only.
        </p>
        {error && <CAlert color="danger" className="py-2">{error}</CAlert>}
        {notice && <CAlert color="info" className="py-2">{notice}</CAlert>}

        {notes.length > 0 && (
          <ul className="list-unstyled mb-3">
            {notes.map((note) => (
              <li key={note.id} className="border-bottom py-2">
                <div className="d-flex align-items-center gap-2 mb-1">
                  <small className="text-body-secondary" suppressHydrationWarning>
                    {new Date(note.created_at).toLocaleString()}
                  </small>
                  {note.delivered_at ? (
                    <CBadge color="success" title={`Delivered ${new Date(note.delivered_at).toLocaleString()}`}>
                      delivered
                    </CBadge>
                  ) : (
                    <CBadge color="secondary">pending</CBadge>
                  )}
                </div>
                <div style={{ whiteSpace: 'pre-wrap' }}>{note.body}</div>
              </li>
            ))}
          </ul>
        )}

        <CForm
          onSubmit={(e) => {
            e.preventDefault();
            postNote();
          }}
        >
          <CFormTextarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            rows={3}
            placeholder="e.g. Keep the public API unchanged; only fix the parser."
            className="mb-2"
          />
          <CButton type="submit" color="primary" size="sm" disabled={posting || !draft.trim()}>
            {posting ? 'Saving…' : 'Add note'}
          </CButton>
        </CForm>
      </CCardBody>
    </CCard>
  );
}
