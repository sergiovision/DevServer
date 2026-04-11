import { query } from '@/lib/db';
import { notFound } from 'next/navigation';
import type { Task, TaskEvent } from '@/lib/types';
import { AgentDetailView } from '@/components/AgentDetailView';

export const dynamic = 'force-dynamic';

interface PageProps {
  params: Promise<{ id: string }>;
}

export default async function AgentDetailPage({ params }: PageProps) {
  const { id } = await params;
  const taskId = parseInt(id);
  if (isNaN(taskId)) notFound();

  let task: Task | null = null;
  let events: TaskEvent[] = [];

  try {
    const taskResult = await query<Task>(
      `SELECT t.*, r.name as repo_name FROM tasks t
       LEFT JOIN repos r ON r.id = t.repo_id
       WHERE t.id = $1 AND t.status = 'running'`,
      [taskId]
    );
    if (taskResult.rows.length === 0) notFound();
    task = taskResult.rows[0];

    const eventsResult = await query<TaskEvent>(
      'SELECT * FROM task_events WHERE task_id = $1 ORDER BY created_at DESC LIMIT 200',
      [taskId]
    );
    events = eventsResult.rows.reverse();
  } catch (err) {
    console.error('Failed to fetch agent detail:', err);
    notFound();
  }

  return <AgentDetailView task={task!} events={events} />;
}
