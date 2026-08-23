'use client';

import React from 'react';

/**
 * A deliberately small markdown renderer for assistant answers.
 *
 * The app has no markdown dependency and this is not a reason to add one: the
 * assistant emits a narrow subset (paragraphs, lists, fenced code, inline code,
 * bold, headings) and a 120-line renderer covers it.
 *
 * It is also the safe option. Everything below builds React elements — there is
 * no `dangerouslySetInnerHTML` anywhere — so model output cannot inject markup
 * no matter what it contains. Links are rendered as plain text for the same
 * reason: an assistant answer is not a place to make an unvetted URL clickable.
 */

const INLINE = /(`[^`]+`|\*\*[^*]+\*\*|\*[^*\n]+\*)/g;

function renderInline(text: string, keyPrefix: string): React.ReactNode[] {
  const nodes: React.ReactNode[] = [];
  const parts = text.split(INLINE);
  parts.forEach((part, i) => {
    if (!part) return;
    const key = `${keyPrefix}-${i}`;
    if (part.startsWith('`') && part.endsWith('`') && part.length > 2) {
      nodes.push(
        <code
          key={key}
          className="px-1 rounded"
          style={{ background: 'var(--cui-tertiary-bg)', fontSize: '0.85em' }}
        >
          {part.slice(1, -1)}
        </code>,
      );
    } else if (part.startsWith('**') && part.endsWith('**') && part.length > 4) {
      nodes.push(<strong key={key}>{part.slice(2, -2)}</strong>);
    } else if (part.startsWith('*') && part.endsWith('*') && part.length > 2) {
      nodes.push(<em key={key}>{part.slice(1, -1)}</em>);
    } else {
      nodes.push(<React.Fragment key={key}>{part}</React.Fragment>);
    }
  });
  return nodes;
}

interface Block {
  kind: 'p' | 'code' | 'ul' | 'ol' | 'h';
  lines: string[];
  level?: number;
  lang?: string;
}

function parseBlocks(src: string): Block[] {
  const blocks: Block[] = [];
  const lines = src.replace(/\r\n/g, '\n').split('\n');
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];

    if (line.trimStart().startsWith('```')) {
      const lang = line.trim().slice(3).trim();
      const body: string[] = [];
      i += 1;
      while (i < lines.length && !lines[i].trimStart().startsWith('```')) {
        body.push(lines[i]);
        i += 1;
      }
      i += 1; // closing fence (or EOF — an unterminated fence still renders)
      blocks.push({ kind: 'code', lines: body, lang });
      continue;
    }

    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    if (heading) {
      blocks.push({ kind: 'h', lines: [heading[2]], level: heading[1].length });
      i += 1;
      continue;
    }

    if (/^\s*[-*+]\s+/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^\s*[-*+]\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*[-*+]\s+/, ''));
        i += 1;
      }
      blocks.push({ kind: 'ul', lines: items });
      continue;
    }

    if (/^\s*\d+[.)]\s+/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^\s*\d+[.)]\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*\d+[.)]\s+/, ''));
        i += 1;
      }
      blocks.push({ kind: 'ol', lines: items });
      continue;
    }

    if (!line.trim()) {
      i += 1;
      continue;
    }

    const para: string[] = [];
    while (
      i < lines.length &&
      lines[i].trim() &&
      !lines[i].trimStart().startsWith('```') &&
      !/^\s*[-*+]\s+/.test(lines[i]) &&
      !/^\s*\d+[.)]\s+/.test(lines[i]) &&
      !/^#{1,6}\s+/.test(lines[i])
    ) {
      para.push(lines[i]);
      i += 1;
    }
    blocks.push({ kind: 'p', lines: para });
  }
  return blocks;
}

export function AskAgentMarkdown({ text }: { text: string }) {
  const blocks = React.useMemo(() => parseBlocks(text || ''), [text]);
  return (
    <div className="assistant-md">
      {blocks.map((block, bi) => {
        const key = `b${bi}`;
        switch (block.kind) {
          case 'code':
            return (
              <pre
                key={key}
                className="p-2 rounded mb-2"
                style={{
                  background: 'var(--cui-tertiary-bg)',
                  border: '1px solid var(--cui-border-color)',
                  overflowX: 'auto',
                  fontSize: '0.82rem',
                }}
              >
                <code>{block.lines.join('\n')}</code>
              </pre>
            );
          case 'h': {
            const size = Math.min(6, Math.max(1, block.level ?? 3));
            return (
              <div
                key={key}
                className="fw-semibold mb-1"
                style={{ fontSize: `${1.15 - size * 0.05}rem` }}
              >
                {renderInline(block.lines[0], key)}
              </div>
            );
          }
          case 'ul':
            return (
              <ul key={key} className="mb-2 ps-3">
                {block.lines.map((item, ii) => (
                  <li key={`${key}-${ii}`}>{renderInline(item, `${key}-${ii}`)}</li>
                ))}
              </ul>
            );
          case 'ol':
            return (
              <ol key={key} className="mb-2 ps-3">
                {block.lines.map((item, ii) => (
                  <li key={`${key}-${ii}`}>{renderInline(item, `${key}-${ii}`)}</li>
                ))}
              </ol>
            );
          default:
            return (
              <p key={key} className="mb-2" style={{ whiteSpace: 'pre-wrap' }}>
                {renderInline(block.lines.join('\n'), key)}
              </p>
            );
        }
      })}
    </div>
  );
}
