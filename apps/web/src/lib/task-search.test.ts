/**
 * Tests for the `/tasks` toolbar search matcher (task-search.ts): term
 * limits, the every-term-must-match rule and case-insensitivity.
 */
import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import type { Task } from './types';
import {
  MAX_SEARCH_TERMS,
  MAX_SEARCH_TERM_LENGTH,
  filterTasksByQuery,
  parseSearchTerms,
  parseStatusFilter,
  taskMatchesStatus,
  taskMatchesTerms,
} from './task-search';

function makeTask(overrides: Partial<Task> = {}): Task {
  return {
    id: 1,
    repo_id: 7,
    task_key: 'DS-101',
    title: 'Fix login redirect',
    description: 'secret-word-only-in-description',
    acceptance: null,
    priority: 2,
    labels: ['frontend'],
    mode: 'autonomous',
    task_type: 'coding',
    claude_mode: 'api',
    agent_vendor: 'glm',
    claude_model: 'glm-4.6',
    max_turns: null,
    status: 'blocked',
    depends_on: [],
    queue_job_id: null,
    skip_verify: false,
    git_flow: 'branch',
    backup_vendor: null,
    backup_model: null,
    created_by: 'operator',
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    repo_name: 'webshop',
    ...overrides,
  };
}

describe('parseSearchTerms', () => {
  // Feature: F-TASK-01
  it('keeps at most eight terms and drops the rest', () => {
    const terms = parseSearchTerms('a b c d e f g h i j');
    assert.equal(terms.length, MAX_SEARCH_TERMS);
    assert.deepEqual(terms, ['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h']);
  });

  // Feature: F-TASK-01
  it('truncates each term to one hundred characters', () => {
    const [term] = parseSearchTerms('x'.repeat(250));
    assert.equal(term.length, MAX_SEARCH_TERM_LENGTH);
  });

  // Feature: F-TASK-01
  it('splits on any whitespace and ignores empty input', () => {
    assert.deepEqual(parseSearchTerms('  Login \t GLM\n'), ['login', 'glm']);
    assert.deepEqual(parseSearchTerms('   '), []);
  });
});

describe('taskMatchesTerms', () => {
  // Feature: F-TASK-02
  it('matches when every term hits some field, even different fields', () => {
    assert.equal(taskMatchesTerms(makeTask(), parseSearchTerms('login glm webshop blocked')), true);
  });

  // Feature: F-TASK-02
  it('rejects the task when any single term matches no field', () => {
    assert.equal(taskMatchesTerms(makeTask(), parseSearchTerms('login nonexistent')), false);
  });

  // Feature: F-TASK-02
  it('does not search the description', () => {
    assert.equal(taskMatchesTerms(makeTask(), parseSearchTerms('secret-word-only-in-description')), false);
  });

  // Feature: F-TASK-02
  it('matches labels, task key and model', () => {
    const task = makeTask();
    assert.equal(taskMatchesTerms(task, ['frontend']), true);
    assert.equal(taskMatchesTerms(task, ['ds-101']), true);
    assert.equal(taskMatchesTerms(task, ['4.6']), true);
  });

  // Feature: F-TASK-02
  it('treats missing optional fields as non-matching rather than crashing', () => {
    const task = makeTask({ repo_name: undefined, claude_model: null, labels: [] });
    assert.equal(taskMatchesTerms(task, ['webshop']), false);
    assert.equal(taskMatchesTerms(task, ['login']), true);
  });

  // Feature: F-TASK-02
  it('is case-insensitive in both query and fields', () => {
    assert.equal(taskMatchesTerms(makeTask(), parseSearchTerms('LOGIN WebShop')), true);
  });
});

describe('filterTasksByQuery', () => {
  // Feature: F-TASK-02
  it('returns every task for an empty query and only matches otherwise', () => {
    const tasks = [makeTask({ id: 1 }), makeTask({ id: 2, title: 'Add billing page' })];
    assert.equal(filterTasksByQuery(tasks, '').length, 2);
    assert.deepEqual(filterTasksByQuery(tasks, 'billing').map((task) => task.id), [2]);
  });
});

describe('status query parameter', () => {
  // Feature: F-TASK-05
  it('parses comma-separated known statuses case-insensitively, without duplicates', () => {
    assert.deepEqual(parseStatusFilter('blocked'), ['blocked']);
    assert.deepEqual(parseStatusFilter(' Failed,blocked,failed '), ['failed', 'blocked']);
  });

  // Feature: F-TASK-05
  it('ignores unknown statuses and missing values instead of hiding every task', () => {
    assert.deepEqual(parseStatusFilter('bogus'), []);
    assert.deepEqual(parseStatusFilter(null), []);
    assert.deepEqual(parseStatusFilter(''), []);
  });

  // Feature: F-TASK-05
  it('keeps only tasks in a requested status, or all when no filter is set', () => {
    assert.equal(taskMatchesStatus(makeTask({ status: 'blocked' }), ['blocked']), true);
    assert.equal(taskMatchesStatus(makeTask({ status: 'running' }), ['blocked']), false);
    assert.equal(taskMatchesStatus(makeTask({ status: 'running' }), []), true);
  });
});
