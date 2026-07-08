'use client';

import { useCallback, useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { CFormCheck, CButtonGroup, CButton } from '@coreui/react-pro';
import { TaskTable } from '@/components/TaskTable';
import { PipelineBoard } from '@/components/PipelineBoard';
import { ImportModal } from '@/components/ImportModal';
import type { Task } from '@/lib/types';

interface TasksViewProps {
  tasks: Task[];
}

type ViewMode = 'grid' | 'board';

export function TasksView({ tasks }: TasksViewProps) {
  const router = useRouter();
  const [view, setView] = useState<ViewMode>('grid');
  const [showRetired, setShowRetired] = useState(false);
  const [groupByRepo, setGroupByRepo] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set());
  const [retiring, setRetiring] = useState(false);

  // The grid may render as one flat table or several per-repo tables. Each
  // table reports the full selection *for its own rows* — replace that
  // slice of the global set so selection survives across groups.
  const replaceGroupSelection = useCallback((groupTasks: Task[], selected: Task[]) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      for (const t of groupTasks) next.delete(t.id);
      for (const t of selected) next.add(t.id);
      return next;
    });
  }, []);

  const handleRetire = useCallback(async () => {
    const ids = Array.from(selectedIds);
    if (ids.length === 0) return;
    setRetiring(true);
    try {
      await Promise.all(
        ids.map((id) =>
          fetch(`/api/tasks/${id}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ status: 'retired' }),
          }),
        ),
      );
      setSelectedIds(new Set());
      router.refresh();
    } finally {
      setRetiring(false);
    }
  }, [selectedIds, router]);

  return (
    <>
      <div className="d-flex justify-content-between align-items-center mb-4 flex-wrap gap-3">
        <div className="d-flex align-items-center gap-3 flex-wrap">
          <Link href="/tasks/new" className="btn btn-primary">
            + New Task
          </Link>
          <CButton color="secondary" variant="outline" onClick={() => setImportOpen(true)}>
            Import
          </CButton>
          {view === 'grid' && (
            <CButton
              color="dark"
              variant="outline"
              disabled={selectedIds.size === 0 || retiring}
              onClick={handleRetire}
            >
              {retiring ? 'Retiring…' : `Retire${selectedIds.size ? ` (${selectedIds.size})` : ''}`}
            </CButton>
          )}
          <CButtonGroup role="group" aria-label="Task view">
            <CButton
              color="secondary"
              variant={view === 'grid' ? undefined : 'outline'}
              size="sm"
              onClick={() => setView('grid')}
            >
              Grid
            </CButton>
            <CButton
              color="secondary"
              variant={view === 'board' ? undefined : 'outline'}
              size="sm"
              onClick={() => setView('board')}
            >
              Board
            </CButton>
          </CButtonGroup>
          {/* Grid-only controls; the board carries its own filters/toggles. */}
          {view === 'grid' && (
            <>
              <CFormCheck
                id="showRetired"
                label="Show Retired"
                checked={showRetired}
                onChange={(e) => setShowRetired(e.target.checked)}
              />
              <CFormCheck
                id="groupByRepo"
                label="Group by Repository"
                checked={groupByRepo}
                onChange={(e) => setGroupByRepo(e.target.checked)}
              />
            </>
          )}
        </div>
        <h2 className="mb-0">Tasks</h2>
      </div>

      {view === 'grid' ? (
        <TaskTable
          tasks={tasks}
          showRetired={showRetired}
          groupByRepo={groupByRepo}
          selectedIds={selectedIds}
          onSelectionChange={replaceGroupSelection}
        />
      ) : (
        <PipelineBoard tasks={tasks} />
      )}

      <ImportModal
        visible={importOpen}
        onClose={() => setImportOpen(false)}
        defaultTarget="task"
        onImported={() => router.refresh()}
      />
    </>
  );
}
