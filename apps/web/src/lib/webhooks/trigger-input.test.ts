import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { parseTriggerCreate, parseTriggerUpdate } from './trigger-input';

describe('parseTriggerCreate', () => {
  it('accepts a minimal valid trigger', () => {
    const result = parseTriggerCreate({ name: ' CI ', repo_id: 1, secret: 's3cret' });
    assert.deepEqual(result, { ok: true, values: { name: 'CI', repo_id: 1, secret: 's3cret' } });
  });

  it('requires a non-empty secret', () => {
    assert.equal(parseTriggerCreate({ name: 'CI', repo_id: 1 }).ok, false);
    assert.equal(parseTriggerCreate({ name: 'CI', repo_id: 1, secret: '   ' }).ok, false);
  });

  it('rejects an invalid key prefix', () => {
    const result = parseTriggerCreate({ name: 'CI', repo_id: 1, secret: 's', task_key_prefix: 'bad prefix!' });
    assert.equal(result.ok, false);
  });
});

describe('parseTriggerUpdate', () => {
  it('accepts a single field', () => {
    assert.deepEqual(parseTriggerUpdate({ enabled: false }), { ok: true, values: { enabled: false } });
  });

  it('ignores unknown keys and rejects an empty update', () => {
    assert.equal(parseTriggerUpdate({ token: 'x', fire_count: 9 }).ok, false);
  });
});
