'use client';

import { useEffect, useState } from 'react';
import { CButton, CFormInput, CFormTextarea, CBadge } from '@coreui/react-pro';
import type { Idea, NodeStatus } from './IdeasView';

interface IdeaEditorProps {
  idea: Idea | null;
  onSave: (patch: Partial<Pick<Idea, 'title' | 'content'>>) => Promise<void>;
  onConvertToTask: () => void;
  onConvertToPlan: () => void;
  convertingPlan: boolean;
  onExpand: () => void;
  onRollup: () => void;
  busy: boolean;
}

const STATUS_COLOR: Record<NodeStatus, string> = {
  draft: 'secondary',
  expanding: 'info',
  ready: 'primary',
  blocked: 'warning',
  running: 'info',
  done: 'success',
  failed: 'danger',
  abandoned: 'dark',
};

export function IdeaEditor({
  idea,
  onSave,
  onConvertToTask,
  onConvertToPlan,
  convertingPlan,
  onExpand,
  onRollup,
  busy,
}: IdeaEditorProps) {
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
      <div className="d-flex align-items-center gap-2 mb-3 flex-wrap">
        <CBadge color={isIdea ? 'info' : 'warning'}>
          {isIdea ? 'Idea' : 'Folder'}
        </CBadge>
        {idea.node_type && (
          <CBadge color="primary" className="text-uppercase">{idea.node_type}</CBadge>
        )}
        {idea.node_type && (
          <CBadge color={STATUS_COLOR[idea.node_status] ?? 'secondary'}>
            {idea.node_status}
          </CBadge>
        )}
        {idea.evaluator_score != null && (
          <CBadge color="light" className="text-dark">score {idea.evaluator_score}</CBadge>
        )}
        {idea.tasked && (
          <CBadge color="success">Tasked{idea.task_id ? ` (#${idea.task_id})` : ''}</CBadge>
        )}
      </div>

      {idea.node_type && (idea.expand_reason || idea.stop_reason || idea.rollup_summary) && (
        <div className="alert alert-light border small mb-3">
          {idea.expand_reason && (
            <div><strong>Expand:</strong> {idea.expand_reason}</div>
          )}
          {idea.stop_reason && (
            <div><strong>Stop:</strong> {idea.stop_reason}</div>
          )}
          {idea.rollup_summary && (
            <details className="mt-1">
              <summary>Rollup summary</summary>
              <pre className="mb-0 mt-1" style={{ whiteSpace: 'pre-wrap' }}>{idea.rollup_summary}</pre>
            </details>
          )}
        </div>
      )}

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

      <div className="d-flex gap-2 flex-wrap">
        <CButton color="primary" onClick={save} disabled={!dirty || saving}>
          {saving ? 'Saving…' : 'Save'}
        </CButton>
        {isIdea && (
          <>
            <CButton
              color="warning"
              onClick={onExpand}
              disabled={busy || idea.node_type === 'leaf'}
              title={
                idea.node_type === 'leaf'
                  ? 'Already a leaf — decompose further with Re-detalize (not in this build)'
                  : 'Classify this node: make it a leaf (creates a task) or split it into subtasks'
              }
            >
              {busy ? 'Working…' : 'Expand / Decompose'}
            </CButton>
            <CButton
              color="dark"
              onClick={onRollup}
              disabled={busy || idea.node_type === 'leaf' || idea.node_type == null}
              title="Synthesise completed children into this node's summary + score"
            >
              Roll up
            </CButton>
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
