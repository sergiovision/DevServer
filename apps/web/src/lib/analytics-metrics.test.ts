/**
 * Tests for analytics-metrics.ts: tokens/second per vendor, average tokens
 * per task (input incl. cache vs output), and that no input shape can leak
 * NaN or Infinity into the dashboard.
 */
import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import {
  computeTokensPerTask,
  computeVendorThroughput,
  roundTo,
  safeRatio,
  toFiniteNumber,
} from './analytics-metrics';

describe('computeVendorThroughput', () => {
  // Feature: F-ANL-01
  it('divides summed output tokens by summed agent-active seconds per vendor', () => {
    const [row] = computeVendorThroughput([
      { vendor: 'anthropic', runs: '3', output_tokens: '12000', active_ms: '60000' },
    ]);
    assert.equal(row.vendor, 'anthropic');
    assert.equal(row.runs, 3);
    assert.equal(row.active_seconds, 60);
    assert.equal(row.output_tokens_per_second, 200);
  });

  // Feature: F-ANL-01
  it('rounds the rate to one decimal and sorts vendors by name', () => {
    const rows = computeVendorThroughput([
      { vendor: 'openai', runs: 1, output_tokens: 1000, active_ms: 3000 },
      { vendor: 'glm', runs: 1, output_tokens: 100, active_ms: 7000 },
    ]);
    assert.deepEqual(rows.map((row) => row.vendor), ['glm', 'openai']);
    assert.equal(rows[0].output_tokens_per_second, 14.3);
    assert.equal(rows[1].output_tokens_per_second, 333.3);
  });

  // Feature: F-ANL-03
  it('reports null, not NaN or Infinity, for a vendor with no active seconds', () => {
    const rows = computeVendorThroughput([
      { vendor: 'google', runs: 2, output_tokens: 500, active_ms: 0 },
      { vendor: 'glm', runs: 0, output_tokens: null, active_ms: null },
    ]);
    for (const row of rows) {
      assert.equal(row.output_tokens_per_second, null);
      assert.equal(Number.isNaN(row.active_seconds), false);
    }
  });

  // Feature: F-ANL-03
  it('labels a missing vendor instead of dropping the row', () => {
    const [row] = computeVendorThroughput([{ vendor: null, runs: 1, output_tokens: 10, active_ms: 1000 }]);
    assert.equal(row.vendor, 'unknown');
    assert.equal(row.output_tokens_per_second, 10);
  });
});

describe('computeTokensPerTask', () => {
  // Feature: F-ANL-02
  it('averages input including cache reads and writes, separately from output', () => {
    const result = computeTokensPerTask([
      {
        vendor: 'anthropic',
        tasks: '2',
        input_tokens: '1000',
        cache_read_tokens: '8000',
        cache_write_tokens: '1000',
        output_tokens: '600',
      },
    ]);
    const [anthropic] = result.by_vendor;
    assert.equal(anthropic.total_input_tokens, 10000);
    assert.equal(anthropic.avg_input_tokens, 5000);
    assert.equal(anthropic.avg_cache_read_tokens, 4000);
    assert.equal(anthropic.avg_output_tokens, 300);
  });

  // Feature: F-ANL-02
  it('weights the overall row by task count, not by vendor', () => {
    const result = computeTokensPerTask([
      { vendor: 'a', tasks: 1, input_tokens: 1000, cache_read_tokens: 0, cache_write_tokens: 0, output_tokens: 100 },
      { vendor: 'b', tasks: 3, input_tokens: 3000, cache_read_tokens: 0, cache_write_tokens: 0, output_tokens: 0 },
    ]);
    assert.equal(result.overall.vendor, 'all');
    assert.equal(result.overall.tasks, 4);
    assert.equal(result.overall.avg_input_tokens, 1000);
    assert.equal(result.overall.avg_output_tokens, 25);
  });

  // Feature: F-ANL-02
  it('rounds per-task averages to whole tokens', () => {
    const [row] = computeTokensPerTask([
      { vendor: 'x', tasks: 3, input_tokens: 1000, cache_read_tokens: 0, cache_write_tokens: 0, output_tokens: 101 },
    ]).by_vendor;
    assert.equal(row.avg_input_tokens, 333);
    assert.equal(row.avg_output_tokens, 34);
  });

  // Feature: F-ANL-03
  it('returns null averages for zero tasks and an empty input', () => {
    const empty = computeTokensPerTask([]);
    assert.deepEqual(empty.by_vendor, []);
    assert.equal(empty.overall.tasks, 0);
    assert.equal(empty.overall.avg_input_tokens, null);
    assert.equal(empty.overall.avg_output_tokens, null);

    const [zero] = computeTokensPerTask([
      { vendor: 'x', tasks: 0, input_tokens: 5, cache_read_tokens: null, cache_write_tokens: undefined, output_tokens: 'n/a' },
    ]).by_vendor;
    assert.equal(zero.avg_input_tokens, null);
    assert.equal(zero.total_output_tokens, 0);
  });
});

describe('numeric helpers', () => {
  // Feature: F-ANL-03
  it('coerces unusable SQL values to zero', () => {
    for (const value of [null, undefined, '', 'abc', Number.NaN, Number.POSITIVE_INFINITY, '-Infinity']) {
      assert.equal(toFiniteNumber(value), 0, String(value));
    }
    assert.equal(toFiniteNumber('12.5'), 12.5);
  });

  // Feature: F-ANL-03
  it('safeRatio is null for zero or negative denominators', () => {
    assert.equal(safeRatio(10, 0), null);
    assert.equal(safeRatio(10, -1), null);
    assert.equal(safeRatio(0, 0), null);
    assert.equal(safeRatio(10, 4), 2.5);
  });

  // Feature: F-ANL-03
  it('roundTo rounds half away from zero', () => {
    assert.equal(roundTo(2.25, 1), 2.3);
    assert.equal(roundTo(-2.25, 1), -2.3);
    assert.equal(roundTo(0.5, 0), 1);
  });
});
