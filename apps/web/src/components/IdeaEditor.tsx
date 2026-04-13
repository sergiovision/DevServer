'use client';

import { useEffect, useState } from 'react';
import { CButton, CFormInput, CFormTextarea, CBadge } from '@coreui/react-pro';
import type { Idea } from './IdeasView';

interface IdeaEditorProps {
  idea: Idea | null;
  onSave: (patch: Partial<Pick<Idea, 'title' | 'content'>>) => Promise<void>;
  onConvertToTask: () => void;
  onConvertToPlan: () => void;
  convertingPlan: boolean;
}

export function IdeaEditor({ idea, onSave, onConvertToTask, onConvertToPlan, convertingPlan }: IdeaEditorProps) {
  const [title, setTitle] = useState('');
  const [content, setContent] = useState('');
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    setTitle(idea?.title ?? '');
    setContent(idea?.content ?? '');
    setDirty(false);
  }, [idea?.id, idea?.title, idea?.content]);

  if (!idea) {
    return (
      <div className="border rounded p-4 text-center text-body-secondary" style={{ minHeight: 400 }}>
        Select a folder or idea from the tree.
      </div>
    );
  }

  const isIdea = idea.kind === 'idea';

  const save = async () => {
    setSaving(true);
    try {
      await onSave({ title, content });
      setDirty(false);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="border rounded p-3" style={{ minHeight: 400 }}>
      <div className="d-flex align-items-center gap-2 mb-3">
        <CBadge color={isIdea ? 'info' : 'warning'}>
          {isIdea ? 'Idea' : 'Folder'}
        </CBadge>
        {idea.tasked && (
          <CBadge color="success">Tasked{idea.task_id ? ` (#${idea.task_id})` : ''}</CBadge>
        )}
      </div>

      <div className="mb-3">
        <label className="form-label">Title</label>
        <CFormInput
          value={title}
          onChange={(e) => {
            setTitle(e.target.value);
            setDirty(true);
          }}
        />
      </div>

      {isIdea && (
        <div className="mb-3">
          <label className="form-label">Content (markdown)</label>
          <CFormTextarea
            value={content}
            onChange={(e) => {
              setContent(e.target.value);
              setDirty(true);
            }}
            rows={14}
            style={{ fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace', fontSize: 13 }}
          />
        </div>
      )}

      <div className="d-flex gap-2">
        <CButton color="primary" onClick={save} disabled={!dirty || saving}>
          {saving ? 'Saving…' : 'Save'}
        </CButton>
        {isIdea && (
          <>
            <CButton
              color="success"
              onClick={onConvertToTask}
              disabled={idea.tasked}
              title={idea.tasked ? 'Already converted' : 'Open New Task dialog with this idea'}
            >
              Convert to Task
            </CButton>
            <CButton
              color="info"
              onClick={onConvertToPlan}
              disabled={convertingPlan}
              title="Generate implementation plan and save to Obsidian"
            >
              {convertingPlan ? 'Generating Plan…' : 'Convert to Plan'}
            </CButton>
          </>
        )}
      </div>
    </div>
  );
}
