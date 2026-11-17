/**
 * A2A push notification configs (`tasks/pushNotificationConfig/*`).
 *
 * A peer registers a webhook URL and DevServer POSTs task status updates to it
 * (delivery lives in `apps/worker/src/services/notify/a2a_backend.py`).
 *
 * SECURITY — this is the one place the gateway accepts a URL that DevServer
 * will later fetch, which makes it a server-side request forgery vector, and a
 * uniquely dangerous one here: the worker sits on `localhost:8000` with **no
 * authentication at all** and exposes `/internal/env` (cleartext secrets) and
 * `/internal/pause`. A peer that could register `http://localhost:8000/...`
 * would turn our own notification fan-out into a confused deputy.
 *
 * So destinations are validated by **resolving the hostname and checking every
 * resolved address** against loopback / private / link-local / CGNAT ranges,
 * not merely by string-matching "localhost". A name that resolves to
 * 127.0.0.1 is rejected on the address, not the label.
 *
 * DNS rebinding — a host that passes here resolving to a private address later
 * — is closed at delivery time: the worker re-resolves, re-applies the same
 * blocklist and connects to the validated IP (`notify/a2a_backend.py`).
 */

import { lookup } from 'dns/promises';
import { BlockList, isIP } from 'net';
import crypto from 'crypto';
import type { QueryResult } from 'pg';
import { pool, query } from '@/lib/db';
import { RPC, RpcError } from './protocol';

export interface PushConfigRow {
  config_id: string;
  url: string;
  token: string | null;
  auth_scheme: string | null;
}

/**
 * Non-public address ranges. KEEP IN SYNC with ``_BLOCKED_NETWORKS`` in
 * ``apps/worker/src/services/notify/a2a_backend.py``, which re-checks at
 * delivery time.
 */
export const BLOCKED_IPV4_SUBNETS: [string, number][] = [
  ['0.0.0.0', 8],        // "this network"
  ['10.0.0.0', 8],       // private
  ['100.64.0.0', 10],    // CGNAT (Tailscale lives here)
  ['127.0.0.0', 8],      // loopback
  ['169.254.0.0', 16],   // link-local + cloud metadata (169.254.169.254)
  ['172.16.0.0', 12],    // private
  ['192.0.0.0', 24],     // IETF protocol assignments
  ['192.0.2.0', 24],     // TEST-NET-1
  ['192.88.99.0', 24],   // 6to4 relay anycast
  ['192.168.0.0', 16],   // private
  ['198.18.0.0', 15],    // benchmarking
  ['198.51.100.0', 24],  // TEST-NET-2
  ['203.0.113.0', 24],   // TEST-NET-3
  ['224.0.0.0', 4],      // multicast
  ['240.0.0.0', 4],      // reserved + broadcast
];

export const BLOCKED_IPV6_SUBNETS: [string, number][] = [
  ['::', 128],           // unspecified
  ['::1', 128],          // loopback
  ['::', 96],            // IPv4-compatible (::a.b.c.d, deprecated)
  ['64:ff9b::', 96],     // NAT64 well-known prefix — embeds an IPv4 target
  ['64:ff9b:1::', 48],   // local-use NAT64
  ['100::', 64],         // discard-only
  ['2001::', 23],        // IETF protocol assignments (incl. Teredo 2001::/32)
  ['2001:db8::', 32],    // documentation
  ['2002::', 16],        // 6to4 — embeds an IPv4 target
  ['fc00::', 7],         // unique-local
  ['fe80::', 10],        // link-local
  ['fec0::', 10],        // site-local (deprecated)
  ['ff00::', 8],         // multicast
];

const BLOCKLIST = (() => {
  const list = new BlockList();
  for (const [net, prefix] of BLOCKED_IPV4_SUBNETS) list.addSubnet(net, prefix, 'ipv4');
  for (const [net, prefix] of BLOCKED_IPV6_SUBNETS) list.addSubnet(net, prefix, 'ipv6');
  return list;
})();

/**
 * True when `ip` is not a public unicast address. An IPv4-mapped IPv6 address
 * (`::ffff:127.0.0.1`, `::ffff:7f00:1`) is checked against the IPv4 ranges by
 * `net.BlockList` itself. Anything that is not a valid IP literal is blocked.
 */
export function isBlockedAddress(ip: string): boolean {
  const v = ip.trim().replace(/^\[|\]$/g, '').split('%')[0];
  const family = isIP(v);
  if (family === 4) return BLOCKLIST.check(v, 'ipv4');
  if (family === 6) return BLOCKLIST.check(v, 'ipv6');
  return true;
}

/** Back-compat wrappers. */
export function isBlockedIPv4(ip: string): boolean {
  return isIP(ip) !== 4 || isBlockedAddress(ip);
}
export function isBlockedIPv6(ip: string): boolean {
  return isIP(ip.split('%')[0]) !== 6 || isBlockedAddress(ip);
}

/** Reject anything that is not a public http(s) endpoint. */
export async function assertSafePushUrl(raw: unknown): Promise<string> {
  if (typeof raw !== 'string' || !raw.trim()) {
    throw new RpcError(RPC.INVALID_PARAMS, 'pushNotificationConfig.url is required');
  }
  let url: URL;
  try {
    url = new URL(raw.trim());
  } catch {
    throw new RpcError(RPC.INVALID_PARAMS, 'pushNotificationConfig.url is not a valid URL');
  }
  if (url.protocol !== 'https:' && url.protocol !== 'http:') {
    throw new RpcError(RPC.INVALID_PARAMS, 'push notification url must be http or https');
  }
  if (url.href.length > 1024) {
    throw new RpcError(RPC.INVALID_PARAMS, 'push notification url is too long');
  }

  if (url.username || url.password) {
    throw new RpcError(RPC.INVALID_PARAMS, 'push notification url must not carry credentials');
  }

  // `URL.hostname` keeps the brackets of an IPv6 literal (`[::1]`), which the
  // resolver would reject; IP literals are checked directly instead.
  const host = url.hostname.replace(/^\[|\]$/g, '');
  let addresses: { address: string; family: number }[];
  try {
    addresses = isIP(host)
      ? [{ address: host, family: isIP(host) }]
      : await lookup(host, { all: true });
  } catch {
    throw new RpcError(
      RPC.INVALID_PARAMS,
      `push notification host does not resolve: ${url.hostname}`,
    );
  }
  if (addresses.length === 0) {
    throw new RpcError(
      RPC.INVALID_PARAMS,
      `push notification host does not resolve: ${url.hostname}`,
    );
  }
  for (const { address } of addresses) {
    if (isBlockedAddress(address)) {
      // Deliberately does not echo the resolved address — no reason to help a
      // caller map the deployment's internal network.
      throw new RpcError(
        RPC.INVALID_PARAMS,
        'push notification url resolves to a non-public address',
      );
    }
  }
  return url.href;
}

function asRecord(v: unknown): Record<string, unknown> {
  return v && typeof v === 'object' && !Array.isArray(v)
    ? (v as Record<string, unknown>)
    : {};
}

function toWireConfig(row: PushConfigRow, taskId: number) {
  return {
    taskId: String(taskId),
    pushNotificationConfig: {
      id: row.config_id,
      url: row.url,
      token: row.token ?? undefined,
      // The credential itself is never echoed back, only the scheme in use.
      authentication: row.auth_scheme
        ? { schemes: [row.auth_scheme] }
        : undefined,
    },
  };
}

const MAX_CONFIGS_PER_TASK = 5;

export async function createPushConfig(
  peerId: number,
  taskId: number,
  params: unknown,
) {
  const p = asRecord(params);
  const cfg = asRecord(p.pushNotificationConfig);
  const url = await assertSafePushUrl(cfg.url);

  const configId =
    typeof cfg.id === 'string' && cfg.id.trim()
      ? cfg.id.trim().slice(0, 64)
      : crypto.randomUUID();

  const auth = asRecord(cfg.authentication);
  const schemes = Array.isArray(auth.schemes) ? auth.schemes : [];
  const scheme =
    typeof schemes[0] === 'string' ? String(schemes[0]).toLowerCase().slice(0, 32) : null;
  const credentials =
    typeof auth.credentials === 'string' ? auth.credentials.slice(0, 512) : null;

  // Count and insert atomically so concurrent creates cannot race past the
  // cap (count-then-insert let N parallel calls all see "4 < 5"). A per-task
  // transaction-scoped advisory lock serialises creators for this task; the
  // INSERT runs as a separate statement after the lock is held, so its
  // READ COMMITTED snapshot sees every sibling committed before it.
  // Replacing an existing (task_id, config_id) is always allowed.
  const client = await pool.connect();
  let res: QueryResult<PushConfigRow>;
  try {
    await client.query('BEGIN');
    await client.query(`SELECT pg_advisory_xact_lock(hashtext('a2a_push_configs'), $1)`, [
      taskId,
    ]);
    res = await client.query<PushConfigRow>(
      `INSERT INTO a2a_push_configs
         (config_id, peer_id, task_id, url, token, auth_scheme, auth_secret)
       SELECT $1::varchar, $2::int, $3::int, $4::varchar, $5::varchar, $6::varchar,
              $7::varchar
        WHERE EXISTS (SELECT 1 FROM a2a_push_configs
                       WHERE task_id = $3 AND config_id = $1)
           OR (SELECT COUNT(*) FROM a2a_push_configs WHERE task_id = $3) < $8::int
       ON CONFLICT (task_id, config_id)
         DO UPDATE SET url = EXCLUDED.url, token = EXCLUDED.token,
                       auth_scheme = EXCLUDED.auth_scheme,
                       auth_secret = EXCLUDED.auth_secret
       RETURNING config_id, url, token, auth_scheme`,
      [
        configId,
        peerId,
        taskId,
        url,
        typeof cfg.token === 'string' ? cfg.token.slice(0, 256) : null,
        scheme,
        credentials,
        MAX_CONFIGS_PER_TASK,
      ],
      );
    await client.query('COMMIT');
  } catch (err) {
    await client.query('ROLLBACK').catch(() => {});
    throw err;
  } finally {
    client.release();
  }
  if (res.rows.length === 0) {
    throw new RpcError(
      RPC.UNSUPPORTED_OPERATION,
      `at most ${MAX_CONFIGS_PER_TASK} push notification configs per task`,
    );
  }
  return toWireConfig(res.rows[0], taskId);
}

export async function getPushConfig(taskId: number, configId: unknown) {
  if (typeof configId !== 'string' || !configId) {
    throw new RpcError(RPC.INVALID_PARAMS, 'pushNotificationConfigId is required');
  }
  const res = await query<PushConfigRow>(
    `SELECT config_id, url, token, auth_scheme FROM a2a_push_configs
      WHERE task_id = $1 AND config_id = $2`,
    [taskId, configId],
  );
  if (res.rows.length === 0) {
    throw new RpcError(RPC.TASK_NOT_FOUND, `push notification config not found: ${configId}`);
  }
  return toWireConfig(res.rows[0], taskId);
}

export async function listPushConfigs(taskId: number) {
  const res = await query<PushConfigRow>(
    `SELECT config_id, url, token, auth_scheme FROM a2a_push_configs
      WHERE task_id = $1 ORDER BY id`,
    [taskId],
  );
  return res.rows.map((r) => toWireConfig(r, taskId));
}

export async function deletePushConfig(taskId: number, configId: unknown) {
  if (typeof configId !== 'string' || !configId) {
    throw new RpcError(RPC.INVALID_PARAMS, 'pushNotificationConfigId is required');
  }
  const res = await query<{ id: number }>(
    `DELETE FROM a2a_push_configs WHERE task_id = $1 AND config_id = $2 RETURNING id`,
    [taskId, configId],
  );
  if (res.rows.length === 0) {
    throw new RpcError(RPC.TASK_NOT_FOUND, `push notification config not found: ${configId}`);
  }
  return null;
}
