/**
 * Tests for linkify.ts: only http(s) URLs become links, trailing punctuation
 * and unbalanced parentheses stay outside the URL, and the segments always
 * reassemble into the original text.
 */
import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { linkHrefs, linkify, type LinkifySegment } from './linkify';

function hrefs(segments: LinkifySegment[]): string[] {
  return segments.flatMap((segment) => (segment.kind === 'link' ? [segment.href] : []));
}

function joined(segments: LinkifySegment[]): string {
  return segments.map((segment) => segment.text).join('');
}

describe('linkify', () => {
  // Feature: F-TASK-03
  it('links http and https URLs between plain text', () => {
    assert.deepEqual(linkify('see https://example.com/a?b=1 and http://h.example now'), [
      { kind: 'text', text: 'see ' },
      { kind: 'link', text: 'https://example.com/a?b=1', href: 'https://example.com/a?b=1' },
      { kind: 'text', text: ' and ' },
      { kind: 'link', text: 'http://h.example', href: 'http://h.example' },
      { kind: 'text', text: ' now' },
    ]);
  });

  // Feature: F-TASK-03
  it('never links javascript, data, vbscript or scheme-relative URLs', () => {
    const input = 'javascript:alert(1) data:text/html,<b>x</b> vbscript:msgbox //evil.example/x file:///etc/passwd';
    const segments = linkify(input);
    assert.deepEqual(hrefs(segments), []);
    assert.equal(joined(segments), input);
  });

  // Feature: F-TASK-03
  it('does not link a scheme glued to a preceding word', () => {
    assert.deepEqual(hrefs(linkify('xhttps://example.com')), []);
  });

  // Feature: F-TASK-03
  it('excludes trailing sentence punctuation from the URL', () => {
    for (const mark of ['.', ',', ';', ':', '!', '?', '?!', '...']) {
      const segments = linkify(`go to https://example.com/path${mark}`);
      assert.deepEqual(hrefs(segments), ['https://example.com/path'], `mark ${mark}`);
      assert.equal(segments[segments.length - 1].text, mark);
    }
  });

  // Feature: F-TASK-03
  it('drops an unbalanced closing parenthesis but keeps a balanced one', () => {
    assert.deepEqual(hrefs(linkify('(see https://a.example/x)')), ['https://a.example/x']);
    assert.deepEqual(hrefs(linkify('(see https://a.example/x).')), ['https://a.example/x']);
    assert.deepEqual(hrefs(linkify('https://en.wikipedia.org/wiki/Foo_(bar)')), [
      'https://en.wikipedia.org/wiki/Foo_(bar)',
    ]);
  });

  // Feature: F-TASK-03
  it('reassembles into the original text', () => {
    const input = 'PR (https://git.example/pr/7), issue https://git.example/i/3. Done!';
    assert.equal(joined(linkify(input)), input);
  });

  // Feature: F-TASK-03
  it('returns one text segment without links and nothing for empty input', () => {
    assert.deepEqual(linkify('plain words'), [{ kind: 'text', text: 'plain words' }]);
    assert.deepEqual(linkify(''), []);
  });
});

describe('linkHrefs', () => {
  // Feature: F-TASK-03
  it('lists each safe URL once in first-seen order', () => {
    assert.deepEqual(
      linkHrefs('a https://b.example, javascript:x https://a.example https://b.example.'),
      ['https://b.example', 'https://a.example'],
    );
  });
});
