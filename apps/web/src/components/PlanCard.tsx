'use client';

import { useState } from 'react';
import { useRouter } from 'next/navigation';
import { CAlert, CBadge, CButton, CCard, CCardBody, CCardHeader } from '@coreui/react-pro';
import type { Task, TaskPlan, TaskRun } from '@/lib/types';

type PlanDecision = 'approve' | 'reject';

interface PlanCardProps {
  task: Pick<Task, 'id' | 'mode' | 'plan_approved_at' | 'plan_rejected_at'>;
  runs: TaskRun[];
}

function stringList(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : [];
}

/**
 * Read a stored plan defensively: `plan_json` is JSONB written by the agent's
 * reply, so a field can be missing or of the wrong type.
 */
function toPlan(raw: unknown): TaskPlan | null {
  if (!raw || typeof raw !== 'object') return null;
  const record = raw as Record<string, unknown>;
  return {
    summary: typeof record.summary === 'string' ? record.summary : '',
    steps: stringList(record.steps),
    files_to_touch: stringList(record.files_to_touch),
    risks: stringList(record.risks),
  };
}

/** The most recently stored plan among the task's runs. */
function latestPlan(runs: TaskRun[]): TaskPlan | null {
  const runsWithPlan = runs.filter((run) => run.plan_json !== null && run.plan_json !== undefined);
  if (runsWithPlan.length === 0) return null;
  const newest = runsWithPlan.reduce((a, b) => (b.id > a.id ? b : a));
  return toPlan(newest.plan_json);
}

function PlanList({ title, items, ordered = false }: { title: string; items: string[]; ordered?: boolean }) {
  if (items.length === 0) return null;
  const ListTag = ordered ? 'ol' : 'ul';
  return (
    <div className="mb-3">
      <div className="fw-semibold small text-body-secondary mb-1">{title}</div>
      <ListTag className="mb-0 ps-3">
        {items.map((item, index) => (
          <li key={index}>{item}</li>
        ))}
      </ListTag>
    </div>
  );
}

/**
 * The plan an interactive task proposed before touching code, with the
 * operator's Approve / Reject decision. The worker waits on this decision;
 * approving lets the agent implement the plan (and only the listed files),
 * rejecting stops the task.
 */
export function PlanCard({ task, runs }: PlanCardProps) {
  const router = useRouter();
  const [submitting, setSubmitting] = useState<PlanDecision | null>(null);
  const [error, setError] = useState('');

  const plan = latestPlan(runs);
  if (task.mode !== 'interactive' || plan === null) return null;

  const decided = Boolean(task.plan_approved_at || task.plan_rejected_at);

  const submitDecision = async (decision: PlanDecision) => {
    setSubmitting(decision);
    setError('');
    try {
      const res = await fetch(`/api/tasks/${task.id}/plan`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ decision }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.error || 'Could not record the decision');
      }
      router.refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not record the decision');
    } finally {
      setSubmitting(null);
    }
  };

  return (
    <CCard className="mb-4 border-warning">
      <CCardHeader className="d-flex align-items-center gap-2">
        <strong>Proposed Plan</strong>
        {task.plan_approved_at && <CBadge color="success">approved</CBadge>}
        {task.plan_rejected_at && <CBadge color="danger">rejected</CBadge>}
        {!decided && <CBadge color="warning">awaiting your decision</CBadge>}
      </CCardHeader>
      <CCardBody>
        {error && <CAlert color="danger" className="py-2">{error}</CAlert>}
        {plan.summary && <p className="mb-3">{plan.summary}</p>}
        <PlanList title="Steps" items={plan.steps} ordered />
        <PlanList title="Files to touch" items={plan.files_to_touch} />
        <PlanList title="Risks" items={plan.risks} />

        {decided ? (
          <small className="text-body-secondary" suppressHydrationWarning>
            {task.plan_approved_at
              ? `Approved ${new Date(task.plan_approved_at).toLocaleString()}.`
              : `Rejected ${new Date(task.plan_rejected_at as string).toLocaleString()}.`}
          </small>
        ) : (
          <div className="d-flex gap-2">
            <CButton
              color="success"
              disabled={submitting !== null}
              onClick={() => submitDecision('approve')}
            >
              {submitting === 'approve' ? 'Approving…' : 'Approve'}
            </CButton>
            <CButton
              color="danger"
              variant="outline"
              disabled={submitting !== null}
              onClick={() => submitDecision('reject')}
            >
              {submitting === 'reject' ? 'Rejecting…' : 'Reject'}
            </CButton>
          </div>
        )}
      </CCardBody>
    </CCard>
  );
}
