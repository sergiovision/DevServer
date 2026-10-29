/**
 * Parse a numeric `[id]` route segment.
 * Returns null unless it is a positive integer, so `"12abc"` or `"-1"` are
 * rejected with a 400 instead of reaching SQL as NaN.
 */
export function parseRouteId(raw: string): number | null {
  const id = Number(raw);
  return Number.isInteger(id) && id > 0 ? id : null;
}
