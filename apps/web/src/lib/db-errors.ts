/**
 * Classifies low-level Postgres / network errors into user-facing messages
 * so the UI can show *why* the database is unreachable instead of a generic
 * "Internal server error".
 *
 * Keep `kind` stable — the frontend NotificationProvider matches on the
 * presence of `db.userMessage` in 503 responses, but logs/dashboards may
 * key on `kind` later.
 */

export type DbErrorKind =
  | 'connection_refused'      // ECONNREFUSED — server not listening
  | 'host_unreachable'        // ENOTFOUND / EAI_AGAIN / EHOSTUNREACH / ENETUNREACH
  | 'connection_timeout'      // ETIMEDOUT or pg connectionTimeoutMillis fired
  | 'connection_reset'        // ECONNRESET
  | 'auth_failed'             // 28P01 invalid_password
  | 'auth_invalid'            // 28000 invalid_authorization_specification (unknown role)
  | 'database_missing'        // 3D000 invalid_catalog_name
  | 'permission_denied'       // 42501 insufficient_privilege
  | 'too_many_connections'    // 53300 too_many_connections
  | 'config_missing'          // PGPASSWORD / connection params unset
  | 'unknown_db';             // anything else that smells like a DB problem

export interface DbErrorInfo {
  kind: DbErrorKind;
  code: string;               // pg sqlstate or node errno (best effort)
  userMessage: string;        // shown in the UI — should be plain English
  hint?: string;              // optional remediation tip ("check that Postgres is running")
}

interface MaybeDbError {
  code?: string;
  errno?: number | string;
  message?: string;
  address?: string;
  port?: number;
}

const dbHostHint = (): string => {
  const host = process.env.PGHOST || '127.0.0.1';
  const port = process.env.PGPORT || '5432';
  return `${host}:${port}`;
};

export function classifyDbError(err: unknown): DbErrorInfo | null {
  if (!err || typeof err !== 'object') return null;
  const e = err as MaybeDbError;
  const code = (e.code ?? '').toString();
  const msg = (e.message ?? '').toString();

  // Node-level network errors come through `pg` unchanged.
  if (code === 'ECONNREFUSED') {
    return {
      kind: 'connection_refused',
      code,
      userMessage: `Cannot connect to PostgreSQL at ${dbHostHint()} — connection refused.`,
      hint: 'Check that Postgres is running and that PGHOST/PGPORT in .env are correct.',
    };
  }
  if (code === 'ENOTFOUND' || code === 'EAI_AGAIN') {
    return {
      kind: 'host_unreachable',
      code,
      userMessage: `Cannot resolve PostgreSQL host "${process.env.PGHOST || '127.0.0.1'}".`,
      hint: 'Check the PGHOST setting in .env — the hostname does not resolve.',
    };
  }
  if (code === 'EHOSTUNREACH' || code === 'ENETUNREACH') {
    return {
      kind: 'host_unreachable',
      code,
      userMessage: `PostgreSQL host ${dbHostHint()} is unreachable from this network.`,
      hint: 'Check VPN / firewall / network connectivity to the database host.',
    };
  }
  if (code === 'ETIMEDOUT' || /timeout/i.test(msg)) {
    return {
      kind: 'connection_timeout',
      code: code || 'ETIMEDOUT',
      userMessage: `Timed out connecting to PostgreSQL at ${dbHostHint()}.`,
      hint: 'The server is reachable but is not responding within 5s — it may be overloaded or blocked by a firewall.',
    };
  }
  if (code === 'ECONNRESET') {
    return {
      kind: 'connection_reset',
      code,
      userMessage: 'PostgreSQL closed the connection unexpectedly.',
      hint: 'The server may have restarted. Retrying usually recovers; if it persists, check Postgres logs.',
    };
  }

  // pg sqlstate codes (5-char strings).
  if (code === '28P01') {
    return {
      kind: 'auth_failed',
      code,
      userMessage: `PostgreSQL rejected the password for user "${process.env.PGUSER || 'devserver'}".`,
      hint: 'Check PGUSER and PGPASSWORD in .env.',
    };
  }
  if (code === '28000') {
    return {
      kind: 'auth_invalid',
      code,
      userMessage: `PostgreSQL rejected the role "${process.env.PGUSER || 'devserver'}".`,
      hint: 'The role does not exist or is not allowed to connect. Check PGUSER and pg_hba.conf.',
    };
  }
  if (code === '3D000') {
    return {
      kind: 'database_missing',
      code,
      userMessage: `Database "${process.env.PGDATABASE || 'devserver'}" does not exist on the server.`,
      hint: 'Run scripts/migrate.sh to create it, or check PGDATABASE in .env.',
    };
  }
  if (code === '42501') {
    return {
      kind: 'permission_denied',
      code,
      userMessage: `User "${process.env.PGUSER || 'devserver'}" lacks permission for this query.`,
      hint: 'Grant the required privileges to the role, or use a role that has them.',
    };
  }
  if (code === '53300') {
    return {
      kind: 'too_many_connections',
      code,
      userMessage: 'PostgreSQL has too many open connections.',
      hint: 'Other clients are saturating the connection limit. Reduce concurrency or raise max_connections.',
    };
  }
  if (code === '57P03' || /the database system is starting up/i.test(msg)) {
    return {
      kind: 'connection_refused',
      code: code || '57P03',
      userMessage: 'PostgreSQL is starting up and not yet accepting connections.',
      hint: 'Wait a few seconds and retry.',
    };
  }
  if (code === '57P01' || code === '57P02' || /admin shutdown|crash shutdown/i.test(msg)) {
    return {
      kind: 'connection_reset',
      code: code || '57P0X',
      userMessage: 'PostgreSQL is shutting down or has been shut down.',
      hint: 'Restart the database server.',
    };
  }

  // Last-resort: pg connection pool timeout surfaces as a plain Error
  // ("timeout exceeded when trying to connect" or "Connection terminated").
  if (/connect.*time|connection terminated|client has encountered a connection error/i.test(msg)) {
    return {
      kind: 'connection_timeout',
      code: code || 'POOL',
      userMessage: `Lost connection to PostgreSQL at ${dbHostHint()}.`,
      hint: 'The pool could not get a working connection. Check that Postgres is up.',
    };
  }

  // pg sqlstate prefix 08 = connection exception family; treat as DB-down.
  if (code.startsWith('08')) {
    return {
      kind: 'connection_reset',
      code,
      userMessage: `PostgreSQL connection error (${code}).`,
      hint: 'Check the database server health and network path.',
    };
  }

  return null;
}

export class DatabaseError extends Error {
  readonly info: DbErrorInfo;
  readonly cause: unknown;
  constructor(info: DbErrorInfo, cause: unknown) {
    super(info.userMessage);
    this.name = 'DatabaseError';
    this.info = info;
    this.cause = cause;
  }
}

export function isDatabaseError(err: unknown): err is DatabaseError {
  return err instanceof DatabaseError;
}
