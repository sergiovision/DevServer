/**
 * `{{placeholder}}` rendering for webhook title / description templates.
 *
 * A placeholder is a dot path into the JSON payload — `{{pull_request.title}}`,
 * `{{commits.0.message}}` — or the name of an `extra` value such as
 * `{{name}}` (the trigger's name). Rendering never evaluates code: paths are
 * plain property lookups on own properties, and the segments that could reach
 * an object's prototype are refused outright.
 *
 * Rules:
 *   - `extra` values take precedence over payload paths of the same name;
 *   - a missing value renders as an empty string;
 *   - strings render as-is, everything else as JSON.
 */

const PLACEHOLDER = /\{\{\s*([^{}]+?)\s*\}\}/g;

/** Path segments that would let a template walk into prototype internals. */
const FORBIDDEN_SEGMENTS = new Set(['__proto__', 'constructor', 'prototype']);

/** Follow a dot path through objects and arrays; undefined when any step is missing. */
function lookupPath(root: unknown, path: string): unknown {
  let current: unknown = root;
  for (const segment of path.split('.')) {
    if (FORBIDDEN_SEGMENTS.has(segment)) return undefined;
    if (current === null || typeof current !== 'object') return undefined;
    if (!Object.prototype.hasOwnProperty.call(current, segment)) return undefined;
    current = (current as Record<string, unknown>)[segment];
  }
  return current;
}

function formatValue(value: unknown): string {
  if (value === undefined || value === null) return '';
  if (typeof value === 'string') return value;
  return JSON.stringify(value);
}

export function renderTemplate(
  template: string,
  payload: unknown,
  extra: Record<string, string> = {},
): string {
  return template.replace(PLACEHOLDER, (_match, rawPath: string) => {
    const path = rawPath.trim();
    if (Object.prototype.hasOwnProperty.call(extra, path)) {
      return extra[path];
    }
    return formatValue(lookupPath(payload, path));
  });
}
