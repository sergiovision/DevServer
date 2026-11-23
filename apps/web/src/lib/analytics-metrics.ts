/**
 * analytics-metrics — turns `/api/analytics` token rows into dashboard
 * metrics: output tokens per second per vendor, and average tokens per task
 * (input vs output).
 *
 * Why it exists: Postgres returns SUM()/COUNT() as strings (numeric/bigint)
 * or NULL, and naive `a / b` on those yields NaN or Infinity the moment a
 * vendor has no recorded active time. Keeping the math here — pure, typed,
 * tested — means the route only runs SQL and the charts only draw numbers.
 *
 * Data source: per-attempt `cost_update` task_events. Their payload carries
 * `duration_ms` (agent-active time of that attempt: the agent call only, not
 * verification or queueing) and, once the worker reports them, the token
 * counts `input_tokens`, `cache_read_tokens`, `cache_write_tokens`,
 * `output_tokens`. Attempts without token counts are excluded in SQL so they
 * cannot dilute a rate with seconds that produced "unknown" tokens.
 *
 * Invariants:
 *  - Every input value goes through `toFiniteNumber`: null, undefined, '',
 *    non-numeric strings, NaN and ±Infinity all become 0.
 *  - A ratio with a zero (or non-positive) denominator is `null`, never NaN
 *    or Infinity, so the UI can render "—" instead of a fake 0 rate.
 *  - "Input" tokens include cache reads and cache writes: the model consumed
 *    them as prompt context, which is what an operator means by input size.
 *  - Rounding: tokens/second to 1 decimal place, per-task averages to whole
 *    tokens.
 */

/** A numeric value as node-postgres hands it over. */
export type SqlNumeric = string | number | null | undefined;

export interface VendorThroughputRow {
  vendor: string | null;
  runs: SqlNumeric;
  output_tokens: SqlNumeric;
  active_ms: SqlNumeric;
}

export interface VendorThroughput {
  vendor: string;
  runs: number;
  output_tokens: number;
  active_seconds: number;
  /** Output tokens ÷ agent-active seconds; null when there are no active seconds. */
  output_tokens_per_second: number | null;
}

export interface VendorTaskTokensRow {
  vendor: string | null;
  tasks: SqlNumeric;
  input_tokens: SqlNumeric;
  cache_read_tokens: SqlNumeric;
  cache_write_tokens: SqlNumeric;
  output_tokens: SqlNumeric;
}

export interface TaskTokenAverages {
  /** Vendor name, or `'all'` for the overall row. */
  vendor: string;
  tasks: number;
  /** Uncached input + cache reads + cache writes, summed over all tasks. */
  total_input_tokens: number;
  total_output_tokens: number;
  avg_input_tokens: number | null;
  avg_cache_read_tokens: number | null;
  avg_output_tokens: number | null;
}

export interface TokensPerTask {
  by_vendor: TaskTokenAverages[];
  overall: TaskTokenAverages;
}

export const OVERALL_VENDOR = 'all';
const UNKNOWN_VENDOR = 'unknown';

/** Coerce a SQL numeric to a finite number; anything unusable is 0. */
export function toFiniteNumber(value: SqlNumeric): number {
  if (value === null || value === undefined || value === '') return 0;
  const parsed = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

/** Round half away from zero to `digits` decimals (avoids `toFixed` string round-trips). */
export function roundTo(value: number, digits: number): number {
  const factor = 10 ** digits;
  return (Math.sign(value) * Math.round(Math.abs(value) * factor)) / factor;
}

/** `numerator / denominator`, or null when the denominator is not positive. */
export function safeRatio(numerator: number, denominator: number): number | null {
  if (!(denominator > 0)) return null;
  const ratio = numerator / denominator;
  return Number.isFinite(ratio) ? ratio : null;
}

function roundedOrNull(value: number | null, digits: number): number | null {
  return value === null ? null : roundTo(value, digits);
}

/** Per-vendor output throughput, sorted by vendor name. */
export function computeVendorThroughput(rows: readonly VendorThroughputRow[]): VendorThroughput[] {
  return rows
    .map((row) => {
      const outputTokens = toFiniteNumber(row.output_tokens);
      const activeSeconds = toFiniteNumber(row.active_ms) / 1000;
      return {
        vendor: row.vendor || UNKNOWN_VENDOR,
        runs: toFiniteNumber(row.runs),
        output_tokens: outputTokens,
        active_seconds: roundTo(activeSeconds, 1),
        output_tokens_per_second: roundedOrNull(safeRatio(outputTokens, activeSeconds), 1),
      };
    })
    .sort((a, b) => a.vendor.localeCompare(b.vendor));
}

interface TokenSums {
  tasks: number;
  input: number;
  cacheRead: number;
  output: number;
}

function averages(vendor: string, sums: TokenSums): TaskTokenAverages {
  return {
    vendor,
    tasks: sums.tasks,
    total_input_tokens: sums.input,
    total_output_tokens: sums.output,
    avg_input_tokens: roundedOrNull(safeRatio(sums.input, sums.tasks), 0),
    avg_cache_read_tokens: roundedOrNull(safeRatio(sums.cacheRead, sums.tasks), 0),
    avg_output_tokens: roundedOrNull(safeRatio(sums.output, sums.tasks), 0),
  };
}

/**
 * Average tokens per task per vendor, plus an overall row weighted by task
 * count (Σtokens ÷ Σtasks — not the mean of the vendor means, which would
 * let a vendor with one task count as much as one with a hundred).
 */
export function computeTokensPerTask(rows: readonly VendorTaskTokensRow[]): TokensPerTask {
  const overall: TokenSums = { tasks: 0, input: 0, cacheRead: 0, output: 0 };
  const byVendor = rows
    .map((row) => {
      const cacheRead = toFiniteNumber(row.cache_read_tokens);
      const sums: TokenSums = {
        tasks: toFiniteNumber(row.tasks),
        input: toFiniteNumber(row.input_tokens) + cacheRead + toFiniteNumber(row.cache_write_tokens),
        cacheRead,
        output: toFiniteNumber(row.output_tokens),
      };
      overall.tasks += sums.tasks;
      overall.input += sums.input;
      overall.cacheRead += sums.cacheRead;
      overall.output += sums.output;
      return averages(row.vendor || UNKNOWN_VENDOR, sums);
    })
    .sort((a, b) => a.vendor.localeCompare(b.vendor));
  return { by_vendor: byVendor, overall: averages(OVERALL_VENDOR, overall) };
}
