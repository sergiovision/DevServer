'use client';

import { useCallback, useState } from 'react';
import { useRouter } from 'next/navigation';
import {
  CSmartTable,
  CBadge,
  CButton,
  CCard,
  CCardHeader,
  CCardBody,
  CCollapse,
} from '@coreui/react-pro';
import type { Task, TaskStatus, TaskType } from '@/lib/types';
import { STATUS_COLORS } from '@/lib/types';
import { taskTypeLabel, TASK_TYPE_BADGE } from '@/lib/task-types';

interface TaskTableProps {
  tasks: Task[];
  showRetired: boolean;
  groupByRepo: boolean;
  /** Ids of the currently checked rows (owned by the parent view). */
  selectedIds: Set<number>;
  /** Reports the full selection for one table's rows so the parent can merge. */
  onSelectionChange: (groupTasks: Task[], selected: Task[]) => void;
}

const columns = [
  { key: 'actions', label: '', _style: { width: '120px' }, sorter: false },
  { key: 'task_key', label: 'Key', _style: { width: '150px' } },
  { key: 'task_type', label: 'Type', _style: { width: '100px' } },
  { key: 'title', label: 'Title' },
  { key: 'repo_name', label: 'Repo', _style: { width: '120px' } },
  { key: 'status', label: 'Status', _style: { width: '110px' } },
  { key: 'max_turns', label: 'Turns', _style: { width: '90px' }, sorter: false },
  { key: 'created_at', label: 'Created', _style: { width: '140px' } },
];

const groupedColumns = [
  { key: 'actions', label: '', _style: { width: '120px' }, sorter: false },
  { key: 'task_key', label: 'Key', _style: { width: '150px' } },
  { key: 'task_type', label: 'Type', _style: { width: '100px' } },
  { key: 'title', label: 'Title' },
  { key: 'status', label: 'Status', _style: { width: '110px' } },
  { key: 'max_turns', label: 'Turns', _style: { width: '90px' }, sorter: false },
  { key: 'created_at', label: 'Created', _style: { width: '140px' } },
];

function statusBadgeColor(status: TaskStatus) {
  return STATUS_COLORS[status] ?? 'secondary';
}

function getScopedColumns(router: ReturnType<typeof useRouter>, handleEnqueue: (id: number) => void, includeRepo: boolean) {
  return {
    status: (item: Task) => (
      <td>
        <CBadge color={statusBadgeColor(item.status as TaskStatus)}>
          {item.status}
        </CBadge>
      </td>
    ),
    task_type: (item: Task) => (
      <td>
        <CBadge color={TASK_TYPE_BADGE[(item.task_type ?? 'coding') as TaskType] ?? 'secondary'}>
          {taskTypeLabel(item.task_type)}
        </CBadge>
      </td>
    ),
    ...(includeRepo ? {
      repo_name: (item: Task) => (
        <td>{item.repo_name || '-'}</td>
      ),
    } : {}),
    max_turns: (item: Task) => (
      <td>
        <CBadge color="secondary" shape="rounded-pill">
          {item.max_turns === null ? '∞' : (item.max_turns ?? 50)}
        </CBadge>
      </td>
    ),
    created_at: (item: Task) => (
      <td suppressHydrationWarning>{new Date(item.created_at).toLocaleDateString()}</td>
    ),
    actions: (item: Task) => (
      <td>
        <CButton
          size="sm"
          color="outline-primary"
          className="me-1"
          onClick={() => router.push(`/tasks/${item.id}`)}
        >
          View
        </CButton>
        {item.status === 'pending' && (
          <CButton
            size="sm"
            color="outline-success"
            onClick={() => handleEnqueue(item.id)}
          >
            Enqueue
          </CButton>
        )}
      </td>
    ),
  };
}

export function TaskTable({ tasks, showRetired, groupByRepo, selectedIds, onSelectionChange }: TaskTableProps) {
  const router = useRouter();
  const [collapsedGroups, setCollapsedGroups] = useState<Record<string, boolean>>({});

  const handleEnqueue = useCallback(async (taskId: number) => {
    await fetch(`/api/tasks/${taskId}/enqueue`, { method: 'POST' });
    router.refresh();
  }, [router]);

  const filteredTasks = showRetired ? tasks : tasks.filter((t) => t.status !== 'retired');

  const toggleGroup = (repoName: string) => {
    setCollapsedGroups((prev) => ({ ...prev, [repoName]: !prev[repoName] }));
  };

  if (groupByRepo) {
    const groups: Record<string, Task[]> = {};
    for (const task of filteredTasks) {
      const key = task.repo_name || '(No Repository)';
      if (!groups[key]) groups[key] = [];
      groups[key].push(task);
    }
    const sortedGroupKeys = Object.keys(groups).sort((a, b) => {
      if (a === '(No Repository)') return 1;
      if (b === '(No Repository)') return -1;
      return a.localeCompare(b);
    });

    return (
      <>
        {sortedGroupKeys.map((repoName) => {
          const groupTasks = groups[repoName];
          const isCollapsed = !!collapsedGroups[repoName];
          return (
            <CCard key={repoName} className="mb-3">
              <CCardHeader
                className="d-flex justify-content-between align-items-center"
                style={{ cursor: 'pointer' }}
                onClick={() => toggleGroup(repoName)}
              >
                <span className="fw-semibold">{repoName}</span>
                <div className="d-flex align-items-center gap-2">
                  <CBadge color="secondary" shape="rounded-pill">
                    {groupTasks.length} task{groupTasks.length !== 1 ? 's' : ''}
                  </CBadge>
                  <span className="text-muted" style={{ fontSize: '0.75rem' }}>
                    {isCollapsed ? '▶' : '▼'}
                  </span>
                </div>
              </CCardHeader>
              <CCollapse visible={!isCollapsed}>
                <CCardBody className="p-0">
                  <CSmartTable
                    items={groupTasks}
                    columns={groupedColumns}
                    tableProps={{ hover: true, responsive: true, striped: true }}
                    columnSorter
                    pagination
                    itemsPerPage={10}
                    itemsPerPageSelect
                    scopedColumns={getScopedColumns(router, handleEnqueue, false)}
                    selectable
                    selected={groupTasks.filter((t) => selectedIds.has(t.id))}
                    onSelectedItemsChange={(items) => onSelectionChange(groupTasks, items as Task[])}
                  />
                </CCardBody>
              </CCollapse>
            </CCard>
          );
        })}
      </>
    );
  }

  return (
    <CSmartTable
      items={filteredTasks}
      columns={columns}
      tableProps={{ hover: true, responsive: true, striped: true }}
      columnSorter
      pagination
      itemsPerPage={20}
      itemsPerPageSelect
      scopedColumns={getScopedColumns(router, handleEnqueue, true)}
      selectable
      selected={filteredTasks.filter((t) => selectedIds.has(t.id))}
      onSelectedItemsChange={(items) => onSelectionChange(filteredTasks, items as Task[])}
    />
  );
}
