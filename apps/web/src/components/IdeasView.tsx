'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import { useRouter } from 'next/navigation';
import { CButton, CSpinner } from '@coreui/react-pro';
import { IdeaTree, type IdeaNode } from './IdeaTree';
import { IdeaEditor } from './IdeaEditor';

export type NodeType = 'goal' | 'subtask' | 'leaf' | null;
export type NodeStatus =
  | 'draft'
  | 'expanding'
  | 'ready'
  | 'blocked'
  | 'running'
  | 'done'
  | 'failed'
  | 'abandoned';

export interface Idea {
  id: number;
  parent_id: number | null;
  kind: 'folder' | 'idea';
  title: string;
  content: string;
  tasked: boolean;
  task_id: number | null;
  sort_order: number;
  // Goal Graph (decomposition) fields — null/default on legacy ideas.
  node_type: NodeType;
  node_status: NodeStatus;
  depth: number;
  evaluator_score: number | null;
  expand_reason: string | null;
  stop_reason: string | null;
  rollup_summary: string | null;
  created_at: string;
  updated_at: string;
}

function buildTree(flat: Idea[]): IdeaNode[] {
  const byId = new Map<number, IdeaNode>();
  flat.forEach((row) => byId.set(row.id, { ...row, children: [] }));
  const roots: IdeaNode[] = [];
  flat.forEach((row) => {
    const node = byId.get(row.id)!;
    if (row.parent_id == null) {
      roots.push(node);
    } else {
      byId.get(row.parent_id)?.children.push(node);
    }
  });
  return roots;
}

export function IdeasView() {
  const router = useRouter();
  const [ideas, setIdeas] = useState<Idea[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [convertingPlan, setConvertingPlan] = useState(false);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const res = await fetch('/api/ideas', { cache: 'no-store' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setIdeas(await res.json());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const selected = useMemo(
    () => ideas?.find((i) => i.id === selectedId) ?? null,
    [ideas, selectedId],
  );

  const tree = useMemo(() => (ideas ? buildTree(ideas) : []), [ideas]);

  const createNode = useCallback(
    async (kind: 'folder' | 'idea') => {
      // Parent is the selected folder, OR the parent of the selected idea, OR root
      let parentId: number | null = null;
      if (selected) {
        parentId = selected.kind === 'folder' ? selected.id : selected.parent_id;
      }
      const defaultTitle = kind === 'folder' ? 'New Folder' : 'New Idea';
      const title = window.prompt(`${kind === 'folder' ? 'Folder' : 'Idea'} name:`, defaultTitle);
      if (!title) return;
      const res = await fetch('/api/ideas', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ parent_id: parentId, kind, title }),
      });
      if (res.ok) {
        const created: Idea = await res.json();
        await load();
        setSelectedId(created.id);
      }
    },
    [selected, load],
  );

  const saveSelected = useCallback(
    async (patch: Partial<Pick<Idea, 'title' | 'content'>>) => {
      if (!selected) return;
      const res = await fetch(`/api/ideas/${selected.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(patch),
      });
      if (res.ok) await load();
    },
    [selected, load],
  );

  const deleteSelected = useCallback(async () => {
    if (!selected) return;
    const label = selected.kind === 'folder' ? 'folder and all contents' : 'idea';
    if (!window.confirm(`Delete this ${label}?`)) return;
    const res = await fetch(`/api/ideas/${selected.id}`, { method: 'DELETE' });
    if (res.ok) {
      setSelectedId(null);
      await load();
    }
  }, [selected, load]);

  const convertToTask = useCallback(() => {
    if (!selected || selected.kind !== 'idea') return;
    const desc = [selected.title, selected.content].filter(Boolean).join('\n\n');
    const qs = new URLSearchParams({
      ideaId: String(selected.id),
      description: desc,
    });
    router.push(`/tasks/new?${qs.toString()}`);
  }, [selected, router]);

  const convertToPlan = useCallback(async () => {
    if (!selected || selected.kind !== 'idea' || !ideas) return;
    // Derive project name from parent folder title
    const parentFolder = selected.parent_id
      ? ideas.find((i) => i.id === selected.parent_id)
      : null;
    const projectName = parentFolder?.title || 'project';
    const desc = [selected.title, selected.content].filter(Boolean).join('\n\n');

    setConvertingPlan(true);
    try {
      const res = await fetch('/api/ideas/generate-plan', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project_name: projectName, description: desc }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => null);
        alert(`Plan generation failed: ${data?.error || res.statusText}`);
        return;
      }
      const plan = await res.json();
      alert(`Plan saved: ${plan.plan_key}`);
    } catch (e) {
      alert(`Plan generation error: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setConvertingPlan(false);
    }
  }, [selected, ideas]);

  // ── Goal Graph: expand one node a level / roll up its children ──
  const expandNode = useCallback(async () => {
    if (!selected || busy) return;
    setBusy(true);
    try {
      const res = await fetch(`/api/goals/${selected.id}/expand`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      });
      const data = await res.json().catch(() => null);
      if (!res.ok) {
        alert(`Expand failed: ${data?.error || res.statusText}`);
        return;
      }
      await load();
      if (data?.outcome === 'leaf') {
        alert(
          data.task_id
            ? `Marked as leaf — task #${data.task_id} created (pending).`
            : `Marked as leaf — no task created (non-coding domain or no repo bound).`,
        );
      }
    } catch (e) {
      alert(`Expand error: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(false);
    }
  }, [selected, busy, load]);

  const rollupNode = useCallback(async () => {
    if (!selected || busy) return;
    setBusy(true);
    try {
      const res = await fetch(`/api/goals/${selected.id}/rollup`, { method: 'POST' });
      const data = await res.json().catch(() => null);
      if (!res.ok) {
        alert(`Rollup not ready: ${data?.error || res.statusText}`);
        return;
      }
      await load();
      alert(`Rolled up: ${data.status} (score ${data.score}).`);
    } catch (e) {
      alert(`Rollup error: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(false);
    }
  }, [selected, busy, load]);

  return (
    <>
      <div className="d-flex justify-content-between align-items-center mb-4">
        <div className="d-flex align-items-center gap-2">
          <CButton color="primary" onClick={() => createNode('folder')}>
            + New Folder
          </CButton>
          <CButton color="primary" onClick={() => createNode('idea')}>
            + New Idea
          </CButton>
          <CButton
            color="outline-danger"
            disabled={!selected}
            onClick={deleteSelected}
          >
            Delete
          </CButton>
        </div>
        <h2 className="mb-0">Ideas</h2>
      </div>

      {error && (
        <div className="alert alert-danger" role="alert">
          Failed to load ideas: {error}
        </div>
      )}

      {ideas === null && !error ? (
        <div className="text-center py-5">
          <CSpinner />
        </div>
      ) : (
        <div className="row g-3">
          <div className="col-md-4">
            <div className="border rounded p-2" style={{ minHeight: 400 }}>
              {tree.length === 0 ? (
                <p className="text-body-secondary small mb-0">
                  No ideas yet. Click &ldquo;+ New Folder&rdquo; or &ldquo;+ New Idea&rdquo; to start.
                </p>
              ) : (
                <IdeaTree
                  nodes={tree}
                  selectedId={selectedId}
                  onSelect={setSelectedId}
                />
              )}
            </div>
          </div>
          <div className="col-md-8">
            <IdeaEditor
              idea={selected}
              onSave={saveSelected}
              onConvertToTask={convertToTask}
              onConvertToPlan={convertToPlan}
              convertingPlan={convertingPlan}
              onExpand={expandNode}
              onRollup={rollupNode}
              busy={busy}
            />
          </div>
        </div>
      )}
    </>
  );
}
