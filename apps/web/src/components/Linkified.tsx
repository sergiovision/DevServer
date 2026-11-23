'use client';

/**
 * Linkified — renders plain text with its http(s) URLs as clickable links.
 *
 * All URL decisions (which schemes are safe, where a URL ends) live in the
 * pure `lib/linkify.ts`; this component only maps segments to React nodes,
 * so text is never injected as HTML. Links open in a new tab with
 * `rel="noopener noreferrer"` so the target page cannot reach back through
 * `window.opener` or learn which task linked to it.
 */
import { Fragment, useMemo } from 'react';
import { linkHrefs, linkify } from '@/lib/linkify';

interface LinkifiedProps {
  text: string | null | undefined;
}

export function Linkified({ text }: LinkifiedProps) {
  const segments = useMemo(() => linkify(text ?? ''), [text]);
  return (
    <>
      {segments.map((segment, index) =>
        segment.kind === 'link' ? (
          <a
            key={index}
            href={segment.href}
            target="_blank"
            rel="noopener noreferrer"
            style={{ wordBreak: 'break-all' }}
          >
            {segment.text}
          </a>
        ) : (
          <Fragment key={index}>{segment.text}</Fragment>
        ),
      )}
    </>
  );
}

interface LinkListProps {
  text: string | null | undefined;
  label?: string;
}

/**
 * The URLs found in text that is shown in an editable field (a textarea
 * cannot hold anchors), rendered as a compact clickable list. Renders
 * nothing when the text has no safe links.
 */
export function LinkList({ text, label = 'Links' }: LinkListProps) {
  const hrefs = useMemo(() => linkHrefs(text ?? ''), [text]);
  if (hrefs.length === 0) return null;
  return (
    <div className="small mt-1 d-flex flex-wrap gap-2 align-items-baseline">
      <span className="text-body-secondary">{label}:</span>
      {hrefs.map((href) => (
        <a key={href} href={href} target="_blank" rel="noopener noreferrer" style={{ wordBreak: 'break-all' }}>
          {href}
        </a>
      ))}
    </div>
  );
}
