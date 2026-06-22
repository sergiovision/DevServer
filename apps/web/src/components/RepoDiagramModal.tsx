'use client';

import { useEffect, useRef, useState } from 'react';
import {
  CModal,
  CModalHeader,
  CModalTitle,
  CModalBody,
  CSpinner,
} from '@coreui/react-pro';

interface RepoDiagramModalProps {
  repoId: number;
  repoName: string;
  visible: boolean;
  onClose: () => void;
}

interface DiagramResponse {
  mermaid: string;
  stats?: { nodes?: number; edges?: number; truncated?: boolean };
  error?: string;
}

// Renders the deterministic Mermaid architecture diagram (module tree) the
// worker generates from repo_map.build_mermaid. Mermaid is imported lazily so
// the heavy library never lands in the SSR bundle or the initial page load.
export function RepoDiagramModal({ repoId, repoName, visible, onClose }: RepoDiagramModalProps) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [stats, setStats] = useState<DiagramResponse['stats'] | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!visible) return;
    let cancelled = false;

    (async () => {
      setLoading(true);
      setError(null);
      setStats(null);
      try {
        const res = await fetch(`/api/repos/${repoId}/diagram`);
        const data: DiagramResponse = await res.json();
        if (!res.ok || data.error) {
          throw new Error(data.error || 'Diagram generation failed');
        }
        if (cancelled) return;
        setStats(data.stats ?? null);

        const mermaid = (await import('mermaid')).default;
        mermaid.initialize({ startOnLoad: false, theme: 'default', securityLevel: 'strict' });
        const { svg } = await mermaid.render(`repo-diagram-${repoId}`, data.mermaid);
        if (cancelled) return;
        if (containerRef.current) {
          containerRef.current.innerHTML = svg;
          // Mermaid sets max-width:100% which shrinks the SVG to fit. Drop it so
          // large trees keep their intrinsic size and the container scrolls.
          const el = containerRef.current.querySelector('svg');
          if (el) {
            el.style.maxWidth = 'none';
            el.style.height = 'auto';
          }
        }
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();

    return () => { cancelled = true; };
  }, [visible, repoId]);

  return (
    <CModal
      fullscreen
      visible={visible}
      onClose={onClose}
      className="repo-diagram-modal"
    >
      <CModalHeader>
        <CModalTitle>Architecture — {repoName}</CModalTitle>
      </CModalHeader>
      <CModalBody className="d-flex flex-column p-0">
        {loading && (
          <div className="text-center py-5">
            <CSpinner /> <span className="ms-2">Generating diagram…</span>
          </div>
        )}
        {error && (
          <div className="alert alert-warning m-3">
            {error}
          </div>
        )}
        {/* Fills the modal and scrolls in both directions when the diagram is
            larger than the viewport. The SVG keeps its intrinsic size so big
            trees stay legible and pan rather than shrink. */}
        <div
          ref={containerRef}
          style={{ flex: 1, overflow: 'auto', textAlign: 'center', padding: '1rem' }}
        />
        {stats && !loading && !error && (
          <div className="text-body-secondary small border-top px-3 py-2">
            {stats.nodes} modules, {stats.edges} edges
            {stats.truncated ? ' (truncated)' : ''} · module tree only (no LLM)
          </div>
        )}
      </CModalBody>
    </CModal>
  );
}
