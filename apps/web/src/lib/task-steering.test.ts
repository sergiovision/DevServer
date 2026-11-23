import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { countTasksNeedingOperator, type OperatorAttentionRow } from './task-steering';

function row(overrides: Partial<OperatorAttentionRow> = {}): OperatorAttentionRow {
  return {
    status: 'running',
    mode: 'interactive',
    has_plan: true,
    plan_approved_at: null,
    plan_rejected_at: null,
    ...overrides,
  };
}

describe('countTasksNeedingOperator', () => {
  // Feature: F-NEED-01
  it('counts blocked tasks and live tasks holding an undecided plan', () => {
    const counts = countTasksNeedingOperator([
      row({ status: 'blocked', mode: 'autonomous', has_plan: false }),
      row({ status: 'blocked' }),
      row({ status: 'running' }),
      row({ status: 'verifying' }),
    ]);
    assert.deepEqual(counts, { blocked: 2, plans_awaiting_decision: 2 });
  });

  // Feature: F-NEED-01
  it('counts a blocked task with an undecided plan once, as blocked', () => {
    assert.deepEqual(countTasksNeedingOperator([row({ status: 'blocked' })]), {
      blocked: 1,
      plans_awaiting_decision: 0,
    });
  });

  // Feature: F-NEED-01
  it('ignores decided plans, missing plans, autonomous tasks and finished tasks', () => {
    const counts = countTasksNeedingOperator([
      row({ plan_approved_at: '2026-01-01T00:00:00Z' }),
      row({ plan_rejected_at: '2026-01-01T00:00:00Z' }),
      row({ has_plan: false }),
      row({ mode: 'autonomous' }),
      row({ status: 'cancelled' }),
      row({ status: 'failed' }),
      row({ status: 'test' }),
    ]);
    assert.deepEqual(counts, { blocked: 0, plans_awaiting_decision: 0 });
  });

  // Feature: F-NEED-01
  it('returns zeros for no rows', () => {
    assert.deepEqual(countTasksNeedingOperator([]), { blocked: 0, plans_awaiting_decision: 0 });
  });
});
