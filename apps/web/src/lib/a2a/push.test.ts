import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { assertSafePushUrl, isBlockedAddress } from './push';
import { makePeerSlug, validatePeerFields } from './peer-validation';

describe('isBlockedAddress', () => {
  it('blocks private, loopback, special-purpose and translated ranges', () => {
    for (const ip of [
      '127.0.0.1', '10.0.0.1', '172.20.1.1', '192.168.0.1', '169.254.169.254',
      '100.64.0.1', '0.0.0.0', '198.18.0.1', '198.19.255.255', '192.0.0.8',
      '224.0.0.1', '255.255.255.255',
      '::1', '::', 'fe80::1', 'fd12::1', '[::1]',
      '::ffff:127.0.0.1', '::ffff:7f00:1', '::127.0.0.1',
      '64:ff9b::7f00:1', '2002:7f00:1::', 'ff02::1', 'garbage',
    ]) {
      assert.equal(isBlockedAddress(ip), true, ip);
    }
  });

  it('allows public unicast', () => {
    for (const ip of ['8.8.8.8', '1.1.1.1', '2606:4700:4700::1111']) {
      assert.equal(isBlockedAddress(ip), false, ip);
    }
  });
});

describe('assertSafePushUrl', () => {
  it('rejects bracketed IPv6 loopback literals without a DNS lookup', async () => {
    await assert.rejects(assertSafePushUrl('http://[::1]:8000/internal/env'), /non-public/);
    await assert.rejects(assertSafePushUrl('http://[::ffff:127.0.0.1]/'), /non-public/);
  });

  it('rejects IPv4 loopback literals and credentials', async () => {
    await assert.rejects(assertSafePushUrl('http://127.0.0.1:8000/'), /non-public/);
    await assert.rejects(assertSafePushUrl('http://u:p@8.8.8.8/'), /credentials/);
  });

  it('accepts a public IP literal', async () => {
    assert.equal(await assertSafePushUrl('https://8.8.8.8/hook'), 'https://8.8.8.8/hook');
  });
});

describe('validatePeerFields', () => {
  it('rejects malformed values with a 400-style error instead of reaching SQL', () => {
    for (const body of [
      { priority: 'high' },
      { max_cost_usd: 'abc' },
      { max_wall_seconds: 1.5 },
      { enabled: 'yes' },
      { task_key_prefix: 'has space' },
      { allowed_repo_ids: [1, 'x'] },
      { daily_task_quota: -1 },
    ]) {
      const r = validatePeerFields(body);
      assert.equal(r.ok, false, JSON.stringify(body));
    }
    assert.equal(validatePeerFields(null).ok, false);
  });

  it('accepts a valid partial update', () => {
    const r = validatePeerFields({ enabled: false, max_cost_usd: null });
    assert.deepEqual(r, { ok: true, values: { enabled: false, max_cost_usd: null } });
  });
});

describe('makePeerSlug', () => {
  it('appends a unique suffix so recreated or punctuation-variant names differ', () => {
    const a = makePeerSlug('CI bot');
    const b = makePeerSlug('ci-bot');
    assert.match(a ?? '', /^ci-bot-[0-9a-f]{8}$/);
    assert.notEqual(a, b);
    assert.equal(makePeerSlug('!!!'), null);
  });
});
