import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import {
  redactSecrets,
  sanitizeForPeer,
  sanitizeJsonForPeer,
  stripHostPaths,
} from './redact';

describe('redactSecrets', () => {
  it('returns empty input unchanged', () => {
    assert.deepEqual(redactSecrets(''), { text: '', found: [] });
  });

  it('redacts an Anthropic key without also matching the OpenAI rule', () => {
    const key = 'sk-ant-api03-' + 'a'.repeat(40);
    const { text, found } = redactSecrets(`key=${key} end`);
    assert.equal(text, 'key=[REDACTED:anthropic_api_key] end');
    assert.deepEqual(found, ['anthropic_api_key']);
  });

  it('redacts every occurrence, not just the first', () => {
    const pat = 'ghp_' + 'A'.repeat(36);
    const { text } = redactSecrets(`${pat} and ${pat}`);
    assert.equal(text, '[REDACTED:github_pat] and [REDACTED:github_pat]');
  });

  it('redacts AWS keys and private key headers', () => {
    const { text, found } = redactSecrets(
      'AKIAABCDEFGHIJKLMNOP\n-----BEGIN RSA PRIVATE KEY-----',
    );
    assert.ok(!text.includes('AKIA'));
    assert.ok(!text.includes('BEGIN RSA'));
    assert.deepEqual(found.sort(), ['aws_access_key', 'private_key']);
  });

  it('redacts only the captured password value, keeping context', () => {
    const { text, found } = redactSecrets('password = "hunter2hunter2"');
    assert.equal(text, 'password = "[REDACTED:hardcoded_password]"');
    assert.deepEqual(found, ['hardcoded_password']);
  });

  it('leaves placeholder passwords alone', () => {
    for (const value of ['changeme', 'your-password-here', 'abcxxxxxxdef']) {
      const input = `password: '${value}'`;
      assert.deepEqual(redactSecrets(input), { text: input, found: [] }, value);
    }
  });

  it('redacts the whole gitea_token match, like the Python rule', () => {
    const { text, found } = redactSecrets(`GITEA_TOKEN=${'a'.repeat(40)} next`);
    assert.equal(text, '[REDACTED:gitea_token] next');
    assert.deepEqual(found, ['gitea_token']);
  });

  it('skips exact placeholders "secret"/"password" but not passwords containing them', () => {
    for (const value of ['secret', 'password']) {
      const input = `password = "${value}"`;
      // "secret" is 6 chars (< 8, never matched); "password" is an exact placeholder.
      assert.deepEqual(redactSecrets(input), { text: input, found: [] }, value);
    }
    const { found } = redactSecrets('password = "mysecret99"');
    assert.deepEqual(found, ['hardcoded_password']);
  });

  it('is stable across repeated calls (no shared regex lastIndex)', () => {
    const input = 'xoxb-1234567890-abcdefghij';
    assert.deepEqual(redactSecrets(input), redactSecrets(input));
    assert.equal(redactSecrets(input).text, '[REDACTED:slack_token]');
  });
});

describe('stripHostPaths', () => {
  it('collapses home directories', () => {
    assert.equal(stripHostPaths('/Users/alice/work/x.ts'), '~/work/x.ts');
    assert.equal(stripHostPaths('/home/bob/repo'), '~/repo');
  });

  it('masks service directories', () => {
    assert.equal(stripHostPaths('at /opt/devserver/data/file.txt'), 'at <path>');
  });
});

describe('sanitizeJsonForPeer', () => {
  it('redacts strings nested in objects and arrays, leaving other types', () => {
    const pat = 'ghp_' + 'C'.repeat(36);
    const out = sanitizeJsonForPeer({
      summary: `uses ${pat}`,
      files_to_touch: ['/Users/alice/repo/a.ts'],
      n: 3,
      ok: true,
      none: null,
    });
    assert.deepEqual(out, {
      summary: 'uses [REDACTED:github_pat]',
      files_to_touch: ['~/repo/a.ts'],
      n: 3,
      ok: true,
      none: null,
    });
  });
});

describe('sanitizeForPeer', () => {
  it('handles null/undefined', () => {
    assert.equal(sanitizeForPeer(null), '');
    assert.equal(sanitizeForPeer(undefined), '');
  });

  it('applies both secret redaction and path stripping', () => {
    const out = sanitizeForPeer('/Users/alice/x ghp_' + 'B'.repeat(36));
    assert.equal(out, '~/x [REDACTED:github_pat]');
  });
});
