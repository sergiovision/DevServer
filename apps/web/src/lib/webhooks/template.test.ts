import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { renderTemplate } from './template';

describe('renderTemplate', () => {
  const payload = {
    action: 'opened',
    issue: { number: 42, title: 'Crash on save', labels: ['bug', 'p1'] },
    items: [{ title: 'first' }, { title: 'second' }],
    flag: true,
  };

  it('replaces dot paths with payload values', () => {
    assert.equal(renderTemplate('Issue #{{issue.number}}: {{issue.title}}', payload), 'Issue #42: Crash on save');
  });

  it('follows array indices', () => {
    assert.equal(renderTemplate('{{items.1.title}}', payload), 'second');
  });

  it('takes extra values such as {{name}} first', () => {
    assert.equal(renderTemplate('Webhook: {{name}}', payload, { name: 'CI' }), 'Webhook: CI');
  });

  it('renders missing values as an empty string', () => {
    assert.equal(renderTemplate('[{{issue.missing}}][{{nope.deeper}}]', payload), '[][]');
  });

  it('JSON-encodes non-string values', () => {
    assert.equal(renderTemplate('{{issue.labels}} {{flag}}', payload), '["bug","p1"] true');
  });

  it('tolerates whitespace inside the braces', () => {
    assert.equal(renderTemplate('{{ action }}', payload), 'opened');
  });

  it('refuses prototype-reaching segments', () => {
    assert.equal(renderTemplate('{{__proto__}}|{{constructor.name}}|{{issue.constructor}}', payload), '||');
  });

  it('ignores inherited properties', () => {
    assert.equal(renderTemplate('{{toString}}', payload), '');
  });
});
