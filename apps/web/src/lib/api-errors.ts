import { NextResponse } from 'next/server';
import { isDatabaseError } from './db-errors';

/**
 * Standard error responder for API route handlers. When the underlying error
 * is a classified DB problem (server down, wrong creds, missing database, …)
 * returns 503 with a structured `db` field that the global fetch interceptor
 * in NotificationProvider renders as a sticky diagnostic banner. Other
 * errors fall back to the generic 500 + "Internal server error" shape used
 * everywhere else.
 */
export function apiErrorResponse(err: unknown, label?: string): NextResponse {
  if (label) {
    console.error(`${label}:`, err);
  } else {
    console.error('API error:', err);
  }

  if (isDatabaseError(err)) {
    return NextResponse.json(
      {
        error: 'Database unavailable',
        db: {
          kind: err.info.kind,
          code: err.info.code,
          userMessage: err.info.userMessage,
          hint: err.info.hint ?? null,
        },
      },
      { status: 503 },
    );
  }

  // Postgres integrity violations (sqlstate class 23: not-null, unique,
  // foreign-key, check). These are request/schema problems, not outages —
  // surface the real constraint message instead of a blank 500 so the UI
  // (and the operator) can see exactly which column/constraint rejected
  // the write.
  if (err && typeof err === 'object' && 'code' in err) {
    const pgErr = err as { code?: string; message?: string; detail?: string };
    if (typeof pgErr.code === 'string' && pgErr.code.startsWith('23')) {
      return NextResponse.json(
        {
          error: pgErr.message || 'Database constraint violation',
          detail: pgErr.detail ?? null,
          code: pgErr.code,
        },
        { status: 400 },
      );
    }
  }

  return NextResponse.json({ error: 'Internal server error' }, { status: 500 });
}
