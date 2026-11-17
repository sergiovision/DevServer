import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import {
  buildArtifacts,
  isAwaitingPlanDecision,
  toA2AState,
  toA2ATask,
  type RunRow,
  type TaskRow,
} from './mapping';

const ORIGIN = 'https://devserver.example';

function makeTask(overrides: Partial<TaskRow> = {}): TaskRow {
  return {
    id: 42,
    task_key: 'A2A-260101000000-abcd',
    repo_id: 3,
    title: 'Do a thing',
    description: 'Do a thing',
    acceptance: null,
    status: 'queued',
    mode: 'autonomous',
    task_type: 'coding',
    git_flow: 'branch',
    priority: 3,
    created_by: 'a2a:orchestrator',
    plan_approved_at: null,
    plan_rejected_at: null,
    abstain_reason: null,
    reality_score: null,
    suspended_reason: null,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:05:00Z',
    repo_name: 'my-repo',
    has_plan: false,
    ...overrides,
  };
}

function makeRun(overrides: Partial<RunRow> = {}): RunRow {
  return {
    attempt: 1,
    status: 'done',
    branch: null,
    pr_url: null,
    cost_usd: '1.25',
    duration_ms: '1000',
    turns: 7,
    error_log: null,
    claude_output: null,
    plan_json: null,
    started_at: null,
    finished_at: null,
    ...overrides,
  };
}

describe('toA2AState', () => {
  const cases: [string, string][] = [
    ['pending', 'submitted'],
    ['queued', 'submitted'],
    ['running', 'working'],
    ['verifying', 'working'],
    ['test', 'completed'],
    ['done', 'completed'],
    ['retired', 'completed'],
    ['failed', 'failed'],
    ['cancelled', 'canceled'],
    ['blocked', 'input-required'],
    ['weird', 'unknown'],
  ];
  for (const [status, state] of cases) {
    it(`maps ${status} → ${state}`, () => {
      assert.equal(toA2AState(makeTask({ status })), state);
    });
  }

  it('reports an abstained task as rejected regardless of status', () => {
    assert.equal(toA2AState(makeTask({ status: 'failed', abstain_reason: 'vague' })), 'rejected');
  });

  it('reports a running task holding an undecided plan as input-required', () => {
    const task = makeTask({ status: 'running', mode: 'interactive', has_plan: true });
    assert.equal(toA2AState(task), 'input-required');
  });
});

describe('isAwaitingPlanDecision', () => {
  it('requires interactive mode and a persisted plan', () => {
    assert.equal(isAwaitingPlanDecision(makeTask({ mode: 'interactive', has_plan: false })), false);
    assert.equal(isAwaitingPlanDecision(makeTask({ mode: 'autonomous', has_plan: true })), false);
    assert.equal(isAwaitingPlanDecision(makeTask({ mode: 'interactive', has_plan: true })), true);
  });

  it('is false once the plan is decided', () => {
    const base = { mode: 'interactive', has_plan: true };
    assert.equal(isAwaitingPlanDecision(makeTask({ ...base, plan_approved_at: 'x' })), false);
    assert.equal(isAwaitingPlanDecision(makeTask({ ...base, plan_rejected_at: 'x' })), false);
  });
});

describe('buildArtifacts', () => {
  it('returns nothing without a run', () => {
    assert.deepEqual(buildArtifacts(makeTask(), null, ORIGIN), []);
  });

  it('emits PR, output and error artifacts, sanitized', () => {
    const run = makeRun({
      status: 'failed',
      pr_url: 'https://git.example/pr/1',
      claude_output: 'see /Users/alice/repo',
      error_log: 'boom ghp_' + 'C'.repeat(36),
    });
    const artifacts = buildArtifacts(makeTask(), run, ORIGIN);
    assert.deepEqual(
      artifacts.map((a) => a.name),
      ['pull-request', 'agent-output', 'error-log'],
    );
    const output = artifacts[1].parts[0];
    assert.equal(output.kind === 'text' && output.text, 'see ~/repo');
    const err = artifacts[2].parts[0];
    assert.equal(err.kind === 'text' && err.text, 'boom [REDACTED:github_pat]');
  });

  it('points patch-mode tasks at the patches listing', () => {
    const artifacts = buildArtifacts(makeTask({ git_flow: 'patch' }), makeRun(), ORIGIN);
    assert.equal(artifacts.length, 1);
    const part = artifacts[0].parts[0];
    assert.equal(part.kind, 'file');
    assert.equal(part.kind === 'file' && part.file.uri, `${ORIGIN}/api/a2a/tasks/42/patches`);
  });
});

describe('toA2ATask', () => {
  it('builds a task with repo context and metadata', () => {
    const task = toA2ATask(makeTask({ status: 'test' }), makeRun(), ORIGIN);
    assert.equal(task.kind, 'task');
    assert.equal(task.id, '42');
    assert.equal(task.contextId, 'repo-3');
    assert.equal(task.status.state, 'completed');
    assert.equal(task.status.message, undefined);
    assert.equal(task.metadata?.costUsd, 1.25);
    assert.equal(task.metadata?.attempt, 1);
  });

  it('uses no-repo context for repo-less tasks', () => {
    const task = toA2ATask(makeTask({ repo_id: null, task_type: 'research' }), null, ORIGIN);
    assert.equal(task.contextId, 'no-repo');
  });

  it('attaches the plan as a data part when awaiting approval', () => {
    const plan = { steps: ['a', 'b'] };
    const task = toA2ATask(
      makeTask({ status: 'running', mode: 'interactive', has_plan: true }),
      makeRun({ plan_json: plan }),
      ORIGIN,
    );
    assert.equal(task.status.state, 'input-required');
    const parts = task.status.message?.parts ?? [];
    assert.equal(parts.length, 2);
    assert.equal(parts[0].kind, 'text');
    assert.deepEqual(parts[1], { kind: 'data', data: plan });
  });

  it('redacts secrets inside the plan data part', () => {
    const pat = 'ghp_' + 'D'.repeat(36);
    const task = toA2ATask(
      makeTask({ status: 'running', mode: 'interactive', has_plan: true }),
      makeRun({ plan_json: { summary: `token ${pat}` } }),
      ORIGIN,
    );
    const parts = task.status.message?.parts ?? [];
    assert.deepEqual(parts[1], {
      kind: 'data',
      data: { summary: 'token [REDACTED:github_pat]' },
    });
  });

  it('tells a peer without approval rights that the operator decides', () => {
    const task = toA2ATask(
      makeTask({ status: 'running', mode: 'interactive', has_plan: true }),
      makeRun({ plan_json: { steps: [] } }),
      ORIGIN,
      false,
    );
    assert.equal(task.status.state, 'input-required');
    const first = task.status.message?.parts[0];
    assert.ok(first?.kind === 'text' && first.text.includes('operator'));
  });

  it('explains a rejection with the abstain reason', () => {
    const task = toA2ATask(makeTask({ abstain_reason: 'too vague' }), null, ORIGIN);
    assert.equal(task.status.state, 'rejected');
    assert.deepEqual(task.status.message?.parts, [{ kind: 'text', text: 'too vague' }]);
  });
});
