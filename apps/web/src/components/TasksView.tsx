'use client';

import { useState } from 'react';
import Link from 'next/link';
import { CFormCheck } from '@coreui/react-pro';
import { TaskTable } from '@/components/TaskTable';
import type { Task } from '@/lib/types';

interface TasksViewProps {
  tasks: Task[];
}

export function TasksView({ tasks }: TasksViewProps) {
  const [showRetired, setShowRetired] = useState(false);

  return (
    <>
      <div className="d-flex justify-content-between align-items-center mb-4">
        <div className="d-flex align-items-center gap-3">
          <Link href="/tasks/new" className="btn btn-primary">
            + New Task
          </Link>
          <CFormCheck
            id="showRetired"
            label="Show Retired"
            checked={showRetired}
            onChange={(e) => setShowRetired(e.target.checked)}
          />
        </div>
        <h2 className="mb-0">Tasks</h2>
      </div>
      <TaskTable tasks={tasks} showRetired={showRetired} />
    </>
  );
}
