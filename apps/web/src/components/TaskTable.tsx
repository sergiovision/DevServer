'use client';

import React, { useCallback } from 'react';
import { useRouter } from 'next/navigation';
import {
  CSmartTable,
  CBadge,
  CButton,
} from '@coreui/react-pro';
import type { Task, TaskPriority, TaskStatus } from '@/lib/types';
import { PRIORITY_LABELS, PRIORITY_COLORS, STATUS_COLORS } from '@/lib/types';

interface TaskTableProps {
  tasks: Task[];
  showRetired: boolean;
}

const columns = [
  { key: 'actions', label: '', _style: { width: '120px' }, sorter: false },
  { key: 'priority', label: 'Priority', _style: { width: '100px' } },
  { key: 'task_key', label: 'Key', _style: { width: '150px' } },
  { key: 'title', label: 'Title' },
  { key: 'repo_name', label: 'Repo', _style: { width: '120px' } },
  { key: 'status', label: 'Status', _style: { width: '110px' } },
  { key: 'max_turns', label: 'Turns', _style: { width: '90px' }, sorter: false },
  { key: 'created_at', label: 'Created', _style: { width: '140px' } },
];

export function TaskTable({ tasks, showRetired }: TaskTableProps) {
  const router = useRouter();

  const handleEnqueue = useCallback(async (taskId: number) => {
    await fetch(`/api/tasks/${taskId}/enqueue`, { method: 'POST' });
    router.refresh();
  }, [router]);

  const filteredTasks = showRetired ? tasks : tasks.filter((t) => t.status !== 'retired');

  return (
    <CSmartTable
      items={filteredTasks}
      columns={columns}
      tableProps={{ hover: true, responsive: true, striped: true }}
      columnSorter
      pagination
      itemsPerPage={20}
      itemsPerPageSelect
      scopedColumns={{
        priority: (item: Task) => (
          <td>
            <CBadge color={PRIORITY_COLORS[item.priority as TaskPriority]}>
              {PRIORITY_LABELS[item.priority as TaskPriority]}
            </CBadge>
          </td>
        ),
        status: (item: Task) => (
          <td>
            <CBadge color={STATUS_COLORS[item.status as TaskStatus]}>
              {item.status}
            </CBadge>
          </td>
        ),
        repo_name: (item: Task) => (
          <td>{item.repo_name || '-'}</td>
        ),
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
      }}
    />
  );
}
