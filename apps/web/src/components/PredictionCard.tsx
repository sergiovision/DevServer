'use client';

import { useEffect, useState } from 'react';
import {
  CCard,
  CCardBody,
  CCardHeader,
  CBadge,
  CSpinner,
} from '@coreui/react-pro';

interface Similar {
  task_key: string;
  title: string;
  status: string;
  succeeded: boolean;
  similarity: number;
}

interface Prediction {
  sample_size: number;
  success_probability: number | null;
  avg_duration_ms: number;
  avg_turns: number;
  similar: Similar[];
  basis?: 'similar' | 'repo';
}

interface Props {
  taskId: number;
}

/**
 * Outcome prediction card (migration 010). Forecasts a task's success
 * probability + expected duration/turns. Free tier shows a repo-level
 * baseline (basis='repo'); Pro shows a similar-task forecast with a sample
 * list (basis='similar'). Renders nothing when there's no history.
 */
export function PredictionCard({ taskId }: Props) {
  const [pred, setPred] = useState<Prediction | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch(`/api/tasks/${taskId}/prediction`);
        if (!res.ok) return;
        const data = await res.json();
        if (!cancelled) setPred(data.prediction ?? null);
      } catch {
        /* best-effort — no card on error */
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [taskId]);

  if (loading) {
    return (
      <CCard className="mb-3">
        <CCardHeader><strong>Outcome Forecast</strong></CCardHeader>
        <CCardBody><CSpinner size="sm" /> Analysing past tasks…</CCardBody>
      </CCard>
    );
  }

  if (!pred || !pred.sample_size || pred.success_probability === null) {
    return null;
  }

  const pct = Math.round((pred.success_probability ?? 0) * 100);
  const color = pct >= 70 ? 'success' : pct >= 40 ? 'warning' : 'danger';
  const mins = Math.round(pred.avg_duration_ms / 60000);
  const fromLabel =
    pred.basis === 'repo'
      ? `${pred.sample_size} task${pred.sample_size === 1 ? '' : 's'} in this repo`
      : `${pred.sample_size} similar task${pred.sample_size === 1 ? '' : 's'}`;

  return (
    <CCard className="mb-3">
      <CCardHeader><strong>Outcome Forecast</strong></CCardHeader>
      <CCardBody>
        <div className="mb-2">
          <CBadge color={color} className="me-2">{pct}% success</CBadge>
          <span className="text-body-secondary">
            ~{mins} min · ~{pred.avg_turns} turns · from {fromLabel}
          </span>
        </div>
        {pred.similar.length > 0 && (
          <ul className="small mb-0 ps-3">
            {pred.similar.slice(0, 5).map((s) => (
              <li key={s.task_key}>
                <CBadge color={s.succeeded ? 'success' : 'secondary'} className="me-1">
                  {s.succeeded ? '✓' : '✗'}
                </CBadge>
                <span className="text-body-secondary">
                  {s.task_key} — {s.title} (sim {s.similarity.toFixed(2)})
                </span>
              </li>
            ))}
          </ul>
        )}
      </CCardBody>
    </CCard>
  );
}
