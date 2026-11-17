import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import {
  generateToken,
  hashToken,
  peerCreatedBy,
  peerMayUseRepo,
  peerMayUseTaskType,
  type A2APeer,
} from './auth';
import { isBlockedIPv4, isBlockedIPv6 } from './push';
import { RPC, partsToText, rpcError, rpcResult } from './protocol';

function makePeer(overrides: Partial<A2APeer> = {}): A2APeer {
  return {
    id: 1,
    name: 'Orchestrator',
    slug: 'orchestrator',
    allowed_repo_ids: [3, 5],
    allowed_task_types: ['research', 'coding'],
    require_plan_approval: true,
    force_git_flow: 'patch',
    max_cost_usd: null,
    max_wall_seconds: null,
    default_agent_vendor: 'anthropic',
    default_model: null,
    task_key_prefix: 'A2A',
    priority: 3,
    daily_task_quota: 10,
    tasks_today: 0,
    enabled: true,
    ...overrides,
  };
}

describe('peer tokens', () => {
  it('generates distinct url-safe tokens long enough for the auth gate', () => {
    const a = generateToken();
    const b = generateToken();
    assert.notEqual(a, b);
    assert.match(a, /^[A-Za-z0-9_-]{43}$/);
  });

  it('hashes to stable sha256 hex', () => {
    assert.equal(
      hashToken('abc'),
      'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad',
    );
  });
});

describe('peer scope', () => {
  const peer = makePeer();

  it('allows only listed repos, and repo-less tasks always', () => {
    assert.equal(peerMayUseRepo(peer, 3), true);
    assert.equal(peerMayUseRepo(peer, 4), false);
    assert.equal(peerMayUseRepo(peer, null), true);
  });

  it('allows only listed task types', () => {
    assert.equal(peerMayUseTaskType(peer, 'coding'), true);
    assert.equal(peerMayUseTaskType(peer, 'script'), false);
  });

  it('tags created tasks with the peer slug', () => {
    assert.equal(peerCreatedBy(peer), 'a2a:orchestrator');
  });
});

describe('push URL address screening', () => {
  it('blocks private, loopback, link-local and CGNAT IPv4', () => {
    for (const ip of ['127.0.0.1', '10.1.2.3', '172.16.0.1', '192.168.1.1',
      '169.254.169.254', '100.64.0.1', '0.0.0.0', '224.0.0.1', 'bogus']) {
      assert.equal(isBlockedIPv4(ip), true, ip);
    }
  });

  it('allows public IPv4', () => {
    assert.equal(isBlockedIPv4('8.8.8.8'), false);
    assert.equal(isBlockedIPv4('172.32.0.1'), false);
  });

  it('blocks loopback, link-local, unique-local and mapped-private IPv6', () => {
    for (const ip of ['::1', '::', 'fe80::1', 'fd00::1', '::ffff:127.0.0.1']) {
      assert.equal(isBlockedIPv6(ip), true, ip);
    }
    assert.equal(isBlockedIPv6('2001:4860:4860::8888'), false);
  });
});

describe('protocol helpers', () => {
  it('concatenates only text parts', () => {
    const text = partsToText([
      { kind: 'text', text: 'hello' },
      { kind: 'data', data: { x: 1 } },
      { kind: 'text', text: 'world' },
    ]);
    assert.equal(text, 'hello\n\nworld');
    assert.equal(partsToText('nope'), '');
  });

  it('builds JSON-RPC envelopes', () => {
    assert.deepEqual(rpcResult(7, { ok: 1 }), { jsonrpc: '2.0', id: 7, result: { ok: 1 } });
    assert.deepEqual(rpcError(undefined, RPC.TASK_NOT_FOUND, 'missing'), {
      jsonrpc: '2.0',
      id: null,
      error: { code: -32001, message: 'missing' },
    });
  });
});
