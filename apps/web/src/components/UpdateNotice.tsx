'use client';

/**
 * Footer notice: "DevServer 1.0.21 is available" + copyable update command + Dismiss.
 *
 * Thin shell over `@/lib/update-notice`: it loads `GET /api/version/update`
 * once per mount (the worker caches the GitHub lookup for 6 h, so this is
 * cheap), shows nothing unless `shouldShowUpdateNotice` says so, and
 * dismisses per release via `POST /api/version/update/dismiss` — a later
 * release shows again.
 */

import { useEffect, useState } from 'react';
import { api } from '@/lib/api-client';
import { useAsyncAction, useResource } from '@/lib/use-async';
import {
  detectHostKind,
  shouldShowUpdateNotice,
  updateCommand,
  type HostKind,
  type UpdateNoticeResponse,
} from '@/lib/update-notice';

const COPIED_FLASH_MS = 1500;

export function UpdateNotice() {
  const notice = useResource(
    (signal) => api.get<UpdateNoticeResponse>('/api/version/update', { signal }),
    [],
  );
  // navigator only exists in the browser; resolve after mount so the server
  // render and the first client render agree.
  const [host, setHost] = useState<HostKind>('unix');
  const [copied, setCopied] = useState(false);
  // Hide immediately on Dismiss instead of waiting for a reload.
  const [dismissedHere, setDismissedHere] = useState<string | null>(null);

  useEffect(() => {
    setHost(
      detectHostKind({
        buildVersion: process.env.NEXT_PUBLIC_VERSION,
        platform: navigator.platform,
        userAgent: navigator.userAgent,
      }),
    );
  }, []);

  useEffect(() => {
    if (!copied) return;
    const timer = setTimeout(() => setCopied(false), COPIED_FLASH_MS);
    return () => clearTimeout(timer);
  }, [copied]);

  const dismiss = useAsyncAction(async (build: string) => {
    await api.post('/api/version/update/dismiss', { build });
    setDismissedHere(build);
  });

  const data = notice.data;
  if (!data || !data.latest) return null;
  if (!shouldShowUpdateNotice({ ...data, dismissed_build: dismissedHere ?? data.dismissed_build })) {
    return null;
  }
  const latest = data.latest;
  const command = updateCommand(host);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(command);
      setCopied(true);
    } catch {
      // Clipboard can be unavailable (non-secure origin); the command stays
      // visible and selectable, so nothing else to do.
    }
  };

  return (
    <div className="d-flex align-items-center gap-2 small flex-wrap" role="status">
      <span className="text-body-secondary">
        {data.release_url ? (
          <a href={data.release_url} target="_blank" rel="noopener noreferrer">
            DevServer {latest}
          </a>
        ) : (
          <>DevServer {latest}</>
        )}{' '}
        is available.
      </span>
      <code className="user-select-all px-1">{command}</code>
      <button type="button" className="btn btn-link btn-sm p-0" onClick={() => void copy()}>
        {copied ? 'Copied' : 'Copy'}
      </button>
      <button
        type="button"
        className="btn btn-link btn-sm p-0 text-body-secondary"
        disabled={dismiss.busy}
        onClick={() => void dismiss.run(latest)}
        title={`Hide this notice for ${latest}; a later release will show again`}
      >
        Dismiss
      </button>
    </div>
  );
}
