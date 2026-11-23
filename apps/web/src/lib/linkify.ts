/**
 * linkify — splits plain text into text and link segments so task titles and
 * descriptions can show clickable URLs without rendering any HTML.
 *
 * Why it exists: task descriptions routinely carry issue / PR / doc URLs that
 * operators had to copy-paste. Rendering them via `dangerouslySetInnerHTML`
 * or a permissive autolinker would open an XSS hole (task text comes from
 * webhooks, imports and A2A peers), so this module is the single, pure place
 * that decides what may become an href.
 *
 * Invariants:
 *  - Only absolute `http://` and `https://` URLs are linked. `javascript:`,
 *    `data:`, `vbscript:`, `file:`, scheme-relative `//host` and every other
 *    scheme stay plain text; every candidate is re-checked with `new URL`.
 *  - A URL must start at a word boundary, so `xhttps://…` is not a link.
 *  - Trailing sentence punctuation `.,;:!?` is never part of the URL, and a
 *    trailing `)` is dropped only when it is unbalanced — so
 *    `(see https://a.example/x)` excludes the `)`, while
 *    `https://en.wikipedia.org/wiki/Foo_(bar)` keeps it.
 *  - Concatenating every segment's `text` reproduces the input exactly.
 */

export type LinkifySegment =
  | { kind: 'text'; text: string }
  | { kind: 'link'; text: string; href: string };

// Candidate URLs: scheme + anything up to whitespace or a character that
// never belongs in a URL written in prose. Trimming happens afterwards.
const URL_CANDIDATE = /(?<![A-Za-z0-9+.-])https?:\/\/[^\s<>"'`]+/gi;
const TRAILING_PUNCTUATION = /[.,;:!?]+$/;

function countOf(text: string, character: string): number {
  let count = 0;
  for (const c of text) if (c === character) count += 1;
  return count;
}

/** Strip trailing punctuation and unbalanced closing parentheses. */
export function trimUrlCandidate(candidate: string): string {
  let url = candidate.replace(TRAILING_PUNCTUATION, '');
  while (url.endsWith(')') && countOf(url, ')') > countOf(url, '(')) {
    url = url.slice(0, -1).replace(TRAILING_PUNCTUATION, '');
  }
  return url;
}

/** True only for a parseable absolute http(s) URL with a host. */
export function isSafeHttpUrl(url: string): boolean {
  try {
    const parsed = new URL(url);
    return (parsed.protocol === 'http:' || parsed.protocol === 'https:') && parsed.hostname.length > 0;
  } catch {
    return false;
  }
}

/** Split `text` into ordered text / link segments (adjacent text is merged). */
export function linkify(text: string): LinkifySegment[] {
  const segments: LinkifySegment[] = [];
  const pushText = (value: string) => {
    if (!value) return;
    const previous = segments[segments.length - 1];
    if (previous && previous.kind === 'text') previous.text += value;
    else segments.push({ kind: 'text', text: value });
  };

  let consumedUpTo = 0;
  for (const match of text.matchAll(URL_CANDIDATE)) {
    const start = match.index ?? 0;
    const url = trimUrlCandidate(match[0]);
    if (!url || !isSafeHttpUrl(url)) continue;
    pushText(text.slice(consumedUpTo, start));
    segments.push({ kind: 'link', text: url, href: url });
    consumedUpTo = start + url.length;
  }
  pushText(text.slice(consumedUpTo));
  return segments;
}

/** The distinct safe URLs in `text`, in first-seen order. */
export function linkHrefs(text: string): string[] {
  const seen = new Set<string>();
  for (const segment of linkify(text)) {
    if (segment.kind === 'link') seen.add(segment.href);
  }
  return [...seen];
}
