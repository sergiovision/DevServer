'use client';

import { useState } from 'react';
import Link from 'next/link';
import { CFormCheck, CButtonGroup, CButton } from '@coreui/react-pro';
import { TaskTable } from '@/components/TaskTable';
import { PipelineBoard } from '@/components/PipelineBoard';
import type { Task } from '@/lib/types';

interface TasksViewProps {
  tasks: Task[];
}

type ViewMode = 'grid' | 'board';

export function TasksView({ tasks }: TasksViewProps) {
  const [view, setView] = useState<ViewMode>('grid');
  const [showRetired, setShowRetired] = useState(false);
  const [groupByRepo, setGroupByRepo] = useState(false);

  return (
    <>
      <div className="d-flex justify-content-between align-items-center mb-4 flex-wrap gap-3">
        <div className="d-flex align-items-center gap-3 flex-wrap">
          <Link href="/tasks/new" className="btn btn-primary">
            + New Task
          </Link>
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
        <TaskTable tasks={tasks} showRetired={showRetired} groupByRepo={groupByRepo} />
      ) : (
        <PipelineBoard tasks={tasks} />
      )}
    </>
  );
}
