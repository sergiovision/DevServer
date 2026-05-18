'use client';

/**
 * Top-level App Router error boundary. Safety net for any Server Component
 * page that forgets to wrap its DB calls with `tryDbPage`. Only effective in
 * dev mode — in production Next.js sanitizes Server Component errors to a
 * generic message + digest, so the DatabaseError shape is no longer visible
 * here. For production coverage, pages must use `tryDbPage`.
 */

import { useEffect } from 'react';
import { CAlert, CButton, CCard, CCardBody, CCardHeader } from '@coreui/react-pro';

interface ErrorWithInfo extends Error {
  digest?: string;
  // DatabaseError attaches { kind, code, userMessage, hint } here. Present
  // only in dev — prod sanitization strips it.
  info?: {
    kind: string;
    code: string;
    userMessage: string;
    hint?: string | null;
  };
}

export default function GlobalError({
  error,
  reset,
}: {
  error: ErrorWithInfo;
  reset: () => void;
}) {
  useEffect(() => {
    console.error('Page render error:', error);
  }, [error]);

  const dbInfo =
    error.name === 'DatabaseError' && error.info ? error.info : null;

  if (dbInfo) {
    return (
      <CCard className="border-danger">
        <CCardHeader className="bg-danger text-white">
          Database unavailable
        </CCardHeader>
        <CCardBody>
          <CAlert color="danger" className="mb-3">
            <div style={{ fontWeight: 600 }}>{dbInfo.userMessage}</div>
            {dbInfo.hint && <div className="mt-2">{dbInfo.hint}</div>}
          </CAlert>
          <div className="text-medium-emphasis small mb-3">
            <div><strong>kind:</strong> <code>{dbInfo.kind}</code></div>
            <div><strong>code:</strong> <code>{dbInfo.code}</code></div>
          </div>
          <CButton color="primary" onClick={reset}>Retry</CButton>
        </CCardBody>
      </CCard>
    );
  }

  return (
    <CCard className="border-danger">
      <CCardHeader className="bg-danger text-white">
        Something went wrong
      </CCardHeader>
      <CCardBody>
        <CAlert color="danger" className="mb-3">
          {error.message || 'An unexpected error occurred while rendering this page.'}
        </CAlert>
        {error.digest && (
          <div className="text-medium-emphasis small mb-3">
            <strong>digest:</strong> <code>{error.digest}</code>
          </div>
        )}
        <CButton color="primary" onClick={reset}>Retry</CButton>
      </CCardBody>
    </CCard>
  );
}
