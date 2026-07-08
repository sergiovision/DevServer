'use client';

import { useEffect, useState } from 'react';
import {
  CCard,
  CCardBody,
  CCardHeader,
  CBadge,
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

// The Pro predictor embeds the task text on the worker's CPU, which can take
// a second or more — cache the answer per task so revisiting a task page
// within the TTL paints the card instantly with zero worker roundtrips.
const PREDICTION_TTL_MS = 5 * 60 * 1000;

function readPredictionCache(taskId: number): Prediction | null {
  try {
    const raw = sessionStorage.getItem(`devserver.prediction.${taskId}`);
    if (!raw) return null;
    const { at, data } = JSON.parse(raw);
    if (typeof at !== 'number' || Date.now() - at > PREDICTION_TTL_MS) return null;
    return data ?? null;
  } catch {
    return null;
  }
}

function writePredictionCache(taskId: number, data: Prediction | null): void {
  try {
    sessionStorage.setItem(
      `devserver.prediction.${taskId}`,
      JSON.stringify({ at: Date.now(), data }),
    );
  } catch {
    /* storage unavailable — caching is best-effort */
  }
}

/**
 * Outcome prediction card (migration 010). Forecasts a task's success
 * probability + expected duration/turns. Free tier shows a repo-level
 * baseline (basis='repo'); Pro shows a similar-task forecast with a sample
 * list (basis='similar'). Renders nothing when there's no history, and
 * nothing while loading — the forecast is auxiliary, so it pops in when
 * ready instead of holding a spinner slot in the layout.
 */
export function PredictionCard({ taskId }: Props) {
  const [pred, setPred] = useState<Prediction | null>(null);

  useEffect(() => {
    let cancelled = false;
    const cached = readPredictionCache(taskId);
    if (cached) {
      setPred(cached);
      return;
    }
    (async () => {
      try {
        const res = await fetch(`/api/tasks/${taskId}/prediction`);
        if (!res.ok) return;
        const data = await res.json();
        if (!cancelled) {
          setPred(data.prediction ?? null);
          writePredictionCache(taskId, data.prediction ?? null);
        }
      } catch {
        /* best-effort — no card on error */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [taskId]);

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
