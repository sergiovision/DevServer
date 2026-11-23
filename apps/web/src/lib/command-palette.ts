/**
 * Command palette ranking — pure, no DOM, no network.
 *
 * Why it exists: the palette must feel predictable. Typing `DS-12` should put
 * that task first, `set` should find Settings before a task that merely
 * mentions "reset", and the same query must always give the same order. Keeping
 * the ranking out of the component makes those promises testable.
 *
 * Invariants:
 * - Match strength: exact > prefix > word-prefix > substring > no match.
 *   A task scores the best of its key and its title.
 * - Ties keep input order (pages in sidebar order, then tasks in the order the
 *   caller supplied them) — `Array.prototype.sort` is stable, and the index is
 *   also an explicit tie-break so nothing depends on engine behaviour.
 * - Empty query → every page, no tasks, no Ask Agent row.
 * - A non-empty query always ends with exactly one "Ask Agent: <query>" row,
 *   even when nothing else matched — the palette never dead-ends.
 */

export const MATCH_EXACT = 4;
export const MATCH_PREFIX = 3;
export const MATCH_WORD_PREFIX = 2;
export const MATCH_SUBSTRING = 1;
export const NO_MATCH = 0;

/** Ranked rows (pages + tasks) shown above the Ask Agent row. */
export const DEFAULT_RESULT_LIMIT = 12;

export interface PalettePage {
  name: string;
  href: string;
}

export interface PaletteTask {
  id: number;
  task_key: string;
  title: string;
  status?: string;
}

export type PaletteEntry =
  | { kind: 'page'; id: string; label: string; href: string }
  | {
      kind: 'task';
      id: string;
      label: string;
      href: string;
      taskKey: string;
      status?: string;
    }
  | { kind: 'ask-agent'; id: string; label: string; query: string };

function normalise(text: string): string {
  return text.trim().toLowerCase();
}

/**
 * How well `query` matches `candidate` (both compared case-insensitively).
 * Word boundaries are any non-alphanumeric character, so `plan` word-prefixes
 * "Fix plan-gate timeout" and `12` word-prefixes "DS-12".
 */
export function scoreMatch(query: string, candidate: string): number {
  const needle = normalise(query);
  const haystack = normalise(candidate);
  if (!needle || !haystack) return NO_MATCH;
  if (haystack === needle) return MATCH_EXACT;
  if (haystack.startsWith(needle)) return MATCH_PREFIX;
  const words = haystack.split(/[^a-z0-9]+/).filter(Boolean);
  if (words.some((word) => word.startsWith(needle))) return MATCH_WORD_PREFIX;
  if (haystack.includes(needle)) return MATCH_SUBSTRING;
  return NO_MATCH;
}

function scoreTask(query: string, task: PaletteTask): number {
  return Math.max(scoreMatch(query, task.task_key), scoreMatch(query, task.title));
}

function pageEntry(page: PalettePage): PaletteEntry {
  return { kind: 'page', id: `page:${page.href}`, label: page.name, href: page.href };
}

function taskEntry(task: PaletteTask): PaletteEntry {
  return {
    kind: 'task',
    id: `task:${task.id}`,
    label: `${task.task_key} — ${task.title}`,
    href: `/tasks/${task.id}`,
    taskKey: task.task_key,
    ...(task.status ? { status: task.status } : {}),
  };
}

export function askAgentEntry(query: string): PaletteEntry {
  const trimmed = query.trim();
  return { kind: 'ask-agent', id: 'ask-agent', label: `Ask Agent: ${trimmed}`, query: trimmed };
}

export interface BuildPaletteInput {
  query: string;
  pages: readonly PalettePage[];
  tasks: readonly PaletteTask[];
  limit?: number;
}

/** The palette's rows for `query`, best first, Ask Agent row last. */
export function buildPaletteResults({
  query,
  pages,
  tasks,
  limit = DEFAULT_RESULT_LIMIT,
}: BuildPaletteInput): PaletteEntry[] {
  if (!query.trim()) return pages.map(pageEntry);

  const scored: { entry: PaletteEntry; score: number; index: number }[] = [];
  let index = 0;
  for (const page of pages) {
    const score = scoreMatch(query, page.name);
    if (score > NO_MATCH) scored.push({ entry: pageEntry(page), score, index });
    index += 1;
  }
  for (const task of tasks) {
    const score = scoreTask(query, task);
    if (score > NO_MATCH) scored.push({ entry: taskEntry(task), score, index });
    index += 1;
  }
  scored.sort((a, b) => b.score - a.score || a.index - b.index);

  const ranked = scored.slice(0, Math.max(0, limit)).map((item) => item.entry);
  ranked.push(askAgentEntry(query));
  return ranked;
}

/** Move the highlighted row by `delta`, wrapping at both ends. */
export function moveSelection(current: number, delta: number, count: number): number {
  if (count <= 0) return 0;
  return (((current + delta) % count) + count) % count;
}
