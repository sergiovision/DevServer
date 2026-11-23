import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import {
  MATCH_EXACT,
  MATCH_PREFIX,
  MATCH_SUBSTRING,
  MATCH_WORD_PREFIX,
  NO_MATCH,
  buildPaletteResults,
  moveSelection,
  scoreMatch,
  type PalettePage,
  type PaletteTask,
} from './command-palette';

const PAGES: PalettePage[] = [
  { name: 'Dashboard', href: '/' },
  { name: 'Tasks', href: '/tasks' },
  { name: 'Settings', href: '/settings' },
  { name: 'Agent Peers', href: '/a2a' },
];

const TASKS: PaletteTask[] = [
  { id: 1, task_key: 'DS-120', title: 'Reset the plan gate', status: 'running' },
  { id: 2, task_key: 'DS-12', title: 'Fix settings form', status: 'blocked' },
  { id: 3, task_key: 'OPS-7', title: 'Rotate DS-12 secrets' },
];

describe('scoreMatch', () => {
  // Feature: F-NAV-01
  it('orders exact over prefix over word-prefix over substring', () => {
    assert.equal(scoreMatch('ds-12', 'DS-12'), MATCH_EXACT);
    assert.equal(scoreMatch('ds-1', 'DS-12'), MATCH_PREFIX);
    assert.equal(scoreMatch('plan', 'Reset the plan gate'), MATCH_WORD_PREFIX);
    assert.equal(scoreMatch('lan', 'Reset the plan gate'), MATCH_SUBSTRING);
    assert.equal(scoreMatch('zzz', 'Reset the plan gate'), NO_MATCH);
  });

  // Feature: F-NAV-01
  it('treats an empty query as no match', () => {
    assert.equal(scoreMatch('   ', 'Tasks'), NO_MATCH);
  });
});

describe('buildPaletteResults', () => {
  // Feature: F-NAV-01
  it('puts the exact task key first, then prefix, then word-prefix matches', () => {
    const results = buildPaletteResults({ query: 'DS-12', pages: PAGES, tasks: TASKS });
    assert.deepEqual(
      results.map((entry) => entry.id),
      ['task:2', 'task:1', 'task:3', 'ask-agent'],
    );
  });

  // Feature: F-NAV-01
  it('keeps input order for equal scores (pages before tasks)', () => {
    const results = buildPaletteResults({ query: 'set', pages: PAGES, tasks: TASKS });
    // "Settings" (page, prefix) beats "Fix settings form" (word-prefix) and
    // "Reset the plan gate" (substring).
    assert.deepEqual(
      results.map((entry) => entry.id),
      ['page:/settings', 'task:2', 'task:1', 'ask-agent'],
    );
    const again = buildPaletteResults({ query: 'set', pages: PAGES, tasks: TASKS });
    assert.deepEqual(again, results);
  });

  // Feature: F-NAV-01
  it('returns every page and nothing else for an empty query', () => {
    const results = buildPaletteResults({ query: '  ', pages: PAGES, tasks: TASKS });
    assert.deepEqual(
      results.map((entry) => entry.kind),
      ['page', 'page', 'page', 'page'],
    );
  });

  // Feature: F-NAV-01
  it('caps ranked rows at the limit but still appends Ask Agent', () => {
    const results = buildPaletteResults({ query: 'ds', pages: PAGES, tasks: TASKS, limit: 1 });
    assert.equal(results.length, 2);
    assert.equal(results[1].kind, 'ask-agent');
  });

  // Feature: F-NAV-05
  it('always ends with exactly one Ask Agent row carrying the trimmed query', () => {
    for (const query of ['tasks', 'nothing matches this', 'DS-12']) {
      const results = buildPaletteResults({ query: `  ${query} `, pages: PAGES, tasks: TASKS });
      const last = results[results.length - 1];
      assert.equal(last.kind, 'ask-agent');
      assert.equal(results.filter((entry) => entry.kind === 'ask-agent').length, 1);
      if (last.kind === 'ask-agent') {
        assert.equal(last.query, query);
        assert.equal(last.label, `Ask Agent: ${query}`);
      }
    }
  });

  // Feature: F-NAV-05
  it('offers only the Ask Agent row when nothing matches', () => {
    const results = buildPaletteResults({ query: 'why is my build red', pages: PAGES, tasks: TASKS });
    assert.deepEqual(results.map((entry) => entry.kind), ['ask-agent']);
  });

  // Feature: F-NAV-01
  it('links tasks to their detail page', () => {
    const [first] = buildPaletteResults({ query: 'OPS-7', pages: PAGES, tasks: TASKS });
    assert.equal(first.kind, 'task');
    if (first.kind === 'task') assert.equal(first.href, '/tasks/3');
  });
});

describe('moveSelection', () => {
  // Feature: F-NAV-01
  it('wraps at both ends and survives an empty list', () => {
    assert.equal(moveSelection(2, 1, 3), 0);
    assert.equal(moveSelection(0, -1, 3), 2);
    assert.equal(moveSelection(0, 1, 0), 0);
  });
});
