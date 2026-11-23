/**
 * The post-completion action validator: the only gate between a request body
 * and the JSONB column the worker acts on.
 */
import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import {
  EMPTY_POST_COMPLETION_DRAFT,
  actionToDraft,
  draftToAction,
  describePostCompletionAction,
  parsePostCompletionAction,
  selfReferenceError,
  toJsonbParam,
} from './post-completion-action';

describe('parsePostCompletionAction', () => {
  // Feature: F-PCA-01
  it('accepts a task action', () => {
    assert.deepEqual(parsePostCompletionAction({ kind: 'task', task_id: 12, trigger: 'on_success' }), {
      ok: true,
      value: { kind: 'task', task_id: 12, trigger: 'on_success' },
    });
  });

  // Feature: F-PCA-01
  it('accepts a skill action and trims the name', () => {
    assert.deepEqual(parsePostCompletionAction({ kind: 'skill', skill_name: ' review ', trigger: 'always' }), {
      ok: true,
      value: { kind: 'skill', skill_name: 'review', trigger: 'always' },
    });
  });

  // Feature: F-PCA-01
  it('treats null and undefined as clearing the action', () => {
    assert.deepEqual(parsePostCompletionAction(null), { ok: true, value: null });
    assert.deepEqual(parsePostCompletionAction(undefined), { ok: true, value: null });
  });

  // Feature: F-PCA-01
  it('drops unknown keys so the stored shape is exact', () => {
    const result = parsePostCompletionAction({ kind: 'task', task_id: 3, trigger: 'on_failure', skill_name: 'x', extra: 1 });
    assert.deepEqual(result, { ok: true, value: { kind: 'task', task_id: 3, trigger: 'on_failure' } });
  });

  // Feature: F-PCA-01
  it('rejects the script kind', () => {
    const result = parsePostCompletionAction({ kind: 'script', trigger: 'always', command: 'rm -rf /' });
    assert.equal(result.ok, false);
  });

  // Feature: F-PCA-01
  it('rejects a task action without a valid target id', () => {
    for (const task_id of [undefined, null, 0, -1, 1.5, '12', true]) {
      const result = parsePostCompletionAction({ kind: 'task', task_id, trigger: 'always' });
      assert.equal(result.ok, false, `task_id=${String(task_id)} should be rejected`);
    }
  });

  // Feature: F-PCA-01
  it('rejects a skill action without a skill name', () => {
    for (const skill_name of [undefined, '', '   ', 42]) {
      const result = parsePostCompletionAction({ kind: 'skill', skill_name, trigger: 'always' });
      assert.equal(result.ok, false);
    }
  });

  // Feature: F-PCA-01
  it('rejects a bad or missing trigger', () => {
    for (const trigger of [undefined, 'sometimes', 'ON_SUCCESS', 1]) {
      const result = parsePostCompletionAction({ kind: 'task', task_id: 1, trigger });
      assert.equal(result.ok, false);
    }
  });

  // Feature: F-PCA-01
  it('rejects non-object values', () => {
    for (const value of ['task', 7, [], true]) {
      assert.equal(parsePostCompletionAction(value).ok, false);
    }
  });
});

describe('selfReferenceError', () => {
  // Feature: F-PCA-03
  it('flags a task action that targets its own task', () => {
    assert.ok(selfReferenceError({ kind: 'task', task_id: 5, trigger: 'always' }, 5));
    assert.equal(selfReferenceError({ kind: 'task', task_id: 6, trigger: 'always' }, 5), null);
    assert.equal(selfReferenceError({ kind: 'skill', skill_name: 'review', trigger: 'always' }, 5), null);
    assert.equal(selfReferenceError(null, 5), null);
  });
});

describe('actionToDraft / draftToAction', () => {
  // Feature: F-PCA-01
  it('round-trips a stored action through the form draft', () => {
    for (const stored of [
      { kind: 'task', task_id: 9, trigger: 'on_failure' },
      { kind: 'skill', skill_name: 'review', trigger: 'always' },
    ] as const) {
      assert.deepEqual(draftToAction(actionToDraft(stored)), { ok: true, value: stored });
    }
  });

  // Feature: F-PCA-01
  it('starts malformed or empty stored values as "none", which clears', () => {
    assert.deepEqual(actionToDraft({ kind: 'script', trigger: 'always' }), EMPTY_POST_COMPLETION_DRAFT);
    assert.deepEqual(actionToDraft(null), EMPTY_POST_COMPLETION_DRAFT);
    assert.deepEqual(draftToAction(EMPTY_POST_COMPLETION_DRAFT), { ok: true, value: null });
  });

  // Feature: F-PCA-01
  it('rejects a half-filled draft instead of saving a partial action', () => {
    for (const taskId of ['', 'abc', '1e3', '-4', '0']) {
      const draft = { ...EMPTY_POST_COMPLETION_DRAFT, kind: 'task' as const, taskId };
      assert.equal(draftToAction(draft).ok, false, `taskId=${taskId} should be rejected`);
    }
    assert.equal(draftToAction({ ...EMPTY_POST_COMPLETION_DRAFT, kind: 'skill' }).ok, false);
  });
});

describe('toJsonbParam / describePostCompletionAction', () => {
  // Feature: F-PCA-01
  it('serialises a value for the JSONB column and null for clearing', () => {
    assert.equal(toJsonbParam(null), null);
    assert.deepEqual(JSON.parse(toJsonbParam({ kind: 'task', task_id: 2, trigger: 'always' }) ?? ''), {
      kind: 'task',
      task_id: 2,
      trigger: 'always',
    });
  });

  // Feature: F-PCA-01
  it('describes an action in one line', () => {
    assert.equal(describePostCompletionAction(null), 'None');
    assert.equal(
      describePostCompletionAction({ kind: 'task', task_id: 12, trigger: 'on_success' }),
      'On success → run task #12',
    );
  });
});
