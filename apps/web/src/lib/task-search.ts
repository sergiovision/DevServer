/**
 * task-search — the `/tasks` toolbar search (grid and board views).
 *
 * Why it exists: the task list grows to hundreds of rows and the only filters
 * were repo / vendor dropdowns; finding "that GLM task about the login page"
 * meant scrolling. This module is the pure matcher both views share, so the
 * grid and the board can never disagree about what a query means.
 *
 * Invariants:
 *  - A query is split on whitespace into at most `MAX_SEARCH_TERMS` terms;
 *    extra terms are DROPPED (not merged), and every term longer than
 *    `MAX_SEARCH_TERM_LENGTH` characters is TRUNCATED to that length. Both
 *    bounds keep a pasted blob (a log line, a stack trace) from turning every
 *    keystroke into a quadratic scan over the list.
 *  - Matching is case-insensitive substring matching.
 *  - A task matches only when EVERY term is found in at least one of its list
 *    fields (AND across terms, OR across fields). The description is not a
 *    list field — it is large and absent from some list payloads — so it is
 *    deliberately not searched.
 *  - An empty / whitespace-only query matches everything.
 *  - A `?status=` URL filter (e.g. the sidebar's "Needs You" link to
 *    `/tasks?status=blocked`) is parsed here too: comma-separated, unknown
 *    values ignored, so a stale or hand-typed link never hides every task.
 */
import type { Task, TaskStatus } from '@/lib/types';

export const MAX_SEARCH_TERMS = 8;
export const MAX_SEARCH_TERM_LENGTH = 100;

/** Lower-cased search terms: ≤ MAX_SEARCH_TERMS terms, each ≤ MAX_SEARCH_TERM_LENGTH chars. */
export function parseSearchTerms(query: string): string[] {
  return query
    .toLowerCase()
    .split(/\s+/)
    .filter((term) => term.length > 0)
    .slice(0, MAX_SEARCH_TERMS)
    .map((term) => term.slice(0, MAX_SEARCH_TERM_LENGTH));
}

/** The lower-cased list fields a term may match, one entry per field. */
export function searchableFields(task: Task): string[] {
  const fields: Array<string | null | undefined> = [
    task.task_key,
    task.title,
    task.repo_name,
    task.status,
    task.task_type,
    task.mode,
    task.git_flow,
    task.agent_vendor,
    task.claude_model,
    task.backup_vendor,
    task.backup_model,
    task.created_by,
    ...(Array.isArray(task.labels) ? task.labels : []),
  ];
  return fields
    .filter((field): field is string => typeof field === 'string' && field.length > 0)
    .map((field) => field.toLowerCase());
}

/** True when every term occurs in at least one searchable field of the task. */
export function taskMatchesTerms(task: Task, terms: readonly string[]): boolean {
  if (terms.length === 0) return true;
  const fields = searchableFields(task);
  return terms.every((term) => fields.some((field) => field.includes(term)));
}

/** Convenience wrapper: parse `query` once and filter `tasks` by it. */
export function filterTasksByQuery<T extends Task>(tasks: readonly T[], query: string): T[] {
  const terms = parseSearchTerms(query);
  if (terms.length === 0) return [...tasks];
  return tasks.filter((task) => taskMatchesTerms(task, terms));
}

const KNOWN_STATUSES: ReadonlySet<string> = new Set<TaskStatus>([
  'pending',
  'queued',
  'running',
  'verifying',
  'test',
  'failed',
  'blocked',
  'cancelled',
  'retired',
]);

function isTaskStatus(value: string): value is TaskStatus {
  return KNOWN_STATUSES.has(value);
}

/** Distinct known statuses from a `?status=a,b` value; empty means "no status filter". */
export function parseStatusFilter(raw: string | null | undefined): TaskStatus[] {
  if (!raw) return [];
  const statuses = raw
    .split(',')
    .map((part) => part.trim().toLowerCase())
    .filter(isTaskStatus);
  return [...new Set(statuses)];
}

/** True when no status filter is active or the task's status is one of them. */
export function taskMatchesStatus(task: Task, statuses: readonly TaskStatus[]): boolean {
  return statuses.length === 0 || statuses.includes(task.status);
}
