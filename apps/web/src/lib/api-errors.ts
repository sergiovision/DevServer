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

  return NextResponse.json({ error: 'Internal server error' }, { status: 500 });
}
