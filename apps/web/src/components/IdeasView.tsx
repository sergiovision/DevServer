'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import { useRouter } from 'next/navigation';
import { CButton, CSpinner } from '@coreui/react-pro';
import { IdeaTree, type IdeaNode } from './IdeaTree';
import { IdeaEditor } from './IdeaEditor';
import { ImportModal } from './ImportModal';
import { EmptyState } from '@/components/EmptyState';
import { api, errorMessage, isApiError, type ApiError } from '@/lib/api-client';

// The goal/plan actions word "the route said no" ("… failed: <reason>")
// differently from "the request never completed" ("… error: <reason>").
function isHttpFailure(err: unknown): err is ApiError {
  return isApiError(err) && !err.isNetworkError;
}

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
  const [importOpen, setImportOpen] = useState(false);

  const load = useCallback(async () => {
    try {
      setIdeas(await api.get<Idea[]>('/api/ideas', { cache: 'no-store' }));
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

  // Set of descendant ids for a given idea (used to block cyclic drops).
  const descendantsOf = useCallback(
    (rootId: number): Set<number> => {
      const out = new Set<number>();
      if (!ideas) return out;
      const childrenBy = new Map<number, number[]>();
      ideas.forEach((i) => {
        if (i.parent_id != null) {
          const arr = childrenBy.get(i.parent_id) ?? [];
          arr.push(i.id);
          childrenBy.set(i.parent_id, arr);
        }
      });
      const stack = [rootId];
      while (stack.length) {
        const id = stack.pop()!;
        for (const child of childrenBy.get(id) ?? []) {
          if (!out.has(child)) {
            out.add(child);
            stack.push(child);
          }
        }
      }
      return out;
    },
    [ideas],
  );

  // Is moving `draggedId` under `targetId` (null = root) a legal move?
  const canReparent = useCallback(
    (draggedId: number, targetId: number | null): boolean => {
      if (draggedId === targetId) return false;
      const dragged = ideas?.find((i) => i.id === draggedId);
      if (!dragged) return false;
      // No-op: already has this parent.
      if ((dragged.parent_id ?? null) === (targetId ?? null)) return false;
      // Cannot drop onto one of its own descendants (would create a cycle).
      if (targetId != null && descendantsOf(draggedId).has(targetId)) return false;
      return true;
    },
    [ideas, descendantsOf],
  );

  const reparent = useCallback(
    async (draggedId: number, targetId: number | null) => {
      if (!ideas || !canReparent(draggedId, targetId)) return;
      const prev = ideas;
      // Optimistic update so the tree reflects the move without a full reload.
      setIdeas((cur) =>
        cur
          ? cur.map((i) => (i.id === draggedId ? { ...i, parent_id: targetId } : i))
          : cur,
      );
      try {
        await api.patch(`/api/ideas/${draggedId}`, { parent_id: targetId });
      } catch {
        // Roll back on failure.
        setIdeas(prev);
      }
    },
    [ideas, canReparent],
  );

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
      // A failed create is silently dropped, as before: the tree simply
      // doesn't change.
      const created = await api
        .post<Idea>('/api/ideas', { parent_id: parentId, kind, title })
        .catch(() => null);
      if (created) {
        await load();
        setSelectedId(created.id);
      }
    },
    [selected, load],
  );

  const saveSelected = useCallback(
    async (patch: Partial<Pick<Idea, 'title' | 'content'>>) => {
      if (!selected) return;
      const saved = await api.patch(`/api/ideas/${selected.id}`, patch).then(
        () => true,
        () => false,
      );
      if (saved) await load();
    },
    [selected, load],
  );

  const deleteSelected = useCallback(async () => {
    if (!selected) return;
    const label = selected.kind === 'folder' ? 'folder and all contents' : 'idea';
    if (!window.confirm(`Delete this ${label}?`)) return;
    const deleted = await api.delete(`/api/ideas/${selected.id}`).then(
      () => true,
      () => false,
    );
    if (deleted) {
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
      const plan = await api.post<{ plan_key: string }>('/api/ideas/generate-plan', {
        project_name: projectName,
        description: desc,
      });
      alert(`Plan saved: ${plan.plan_key}`);
    } catch (e) {
      alert(
        isHttpFailure(e)
          ? `Plan generation failed: ${e.message}`
          : `Plan generation error: ${errorMessage(e, String(e))}`,
      );
    } finally {
      setConvertingPlan(false);
    }
  }, [selected, ideas]);

  // ── Goal Graph: expand one node a level / roll up its children ──
  const expandNode = useCallback(async () => {
    if (!selected || busy) return;
    setBusy(true);
    try {
      const data = await api.post<{ outcome?: string; task_id?: number | null } | null>(
        `/api/goals/${selected.id}/expand`,
        {},
      );
      await load();
      if (data?.outcome === 'leaf') {
        alert(
          data.task_id
            ? `Marked as leaf — task #${data.task_id} created (pending).`
            : `Marked as leaf — no task created (non-coding domain or no repo bound).`,
        );
      }
    } catch (e) {
      alert(
        isHttpFailure(e)
          ? `Expand failed: ${e.message}`
          : `Expand error: ${errorMessage(e, String(e))}`,
      );
    } finally {
      setBusy(false);
    }
  }, [selected, busy, load]);

  const rollupNode = useCallback(async () => {
    if (!selected || busy) return;
    setBusy(true);
    try {
      const data = await api.post<{ status: string; score: number }>(`/api/goals/${selected.id}/rollup`);
      await load();
      alert(`Rolled up: ${data.status} (score ${data.score}).`);
    } catch (e) {
      alert(
        isHttpFailure(e)
          ? `Rollup not ready: ${e.message}`
          : `Rollup error: ${errorMessage(e, String(e))}`,
      );
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
          <CButton color="secondary" variant="outline" onClick={() => setImportOpen(true)}>
            Import
          </CButton>
        </div>
        <h2 className="mb-0">Ideas</h2>
      </div>

      <ImportModal
        visible={importOpen}
        onClose={() => setImportOpen(false)}
        defaultTarget="idea"
        onImported={load}
      />

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
                <EmptyState
                  compact
                  title="No ideas yet"
                  hint={<>Click &ldquo;+ New Folder&rdquo; or &ldquo;+ New Idea&rdquo; to start.</>}
                />
              ) : (
                <IdeaTree
                  nodes={tree}
                  selectedId={selectedId}
                  onSelect={setSelectedId}
                  onReparent={reparent}
                  canReparent={canReparent}
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
