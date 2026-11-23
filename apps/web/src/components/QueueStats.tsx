'use client';

import React from 'react';
import { CRow, CCol, CWidgetStatsF } from '@coreui/react-pro';
import CIcon from '@coreui/icons-react';
import { cilMediaPlay, cilClock, cilCheckAlt, cilXCircle, cilMediaPause } from '@coreui/icons';
import type { QueueStatsResponse } from '@/lib/types';
import { api } from '@/lib/api-client';
import { useResource } from '@/lib/use-async';

export function QueueStats() {
  // A failed poll keeps the last stats on screen.
  const { data: stats } = useResource<QueueStatsResponse | null>(
    (signal) => api.get<QueueStatsResponse | null>('/api/queue', { signal }),
    [],
    { pollMs: 5000 },
  );

  if (!stats) return null;

  return (
    <CRow className="mb-4">
      <CCol sm={6} lg={2}>
        <CWidgetStatsF
          className="mb-3"
          color="success"
          icon={<CIcon icon={cilMediaPlay} height={24} />}
          title="Active"
          value={String(stats.active)}
        />
      </CCol>
      <CCol sm={6} lg={2}>
        <CWidgetStatsF
          className="mb-3"
          color="info"
          icon={<CIcon icon={cilClock} height={24} />}
          title="Waiting"
          value={String(stats.waiting)}
        />
      </CCol>
      <CCol sm={6} lg={2}>
        <CWidgetStatsF
          className="mb-3"
          color="primary"
          icon={<CIcon icon={cilCheckAlt} height={24} />}
          title="Completed"
          value={String(stats.completed)}
        />
      </CCol>
      <CCol sm={6} lg={2}>
        <CWidgetStatsF
          className="mb-3"
          color="danger"
          icon={<CIcon icon={cilXCircle} height={24} />}
          title="Failed"
          value={String(stats.failed)}
        />
      </CCol>
      <CCol sm={6} lg={2}>
        <CWidgetStatsF
          className="mb-3"
          color="warning"
          icon={<CIcon icon={cilClock} height={24} />}
          title="Delayed"
          value={String(stats.delayed)}
        />
      </CCol>
      <CCol sm={6} lg={2}>
        <CWidgetStatsF
          className="mb-3"
          color={stats.paused ? 'danger' : 'secondary'}
          icon={<CIcon icon={cilMediaPause} height={24} />}
          title="Paused"
          value={stats.paused ? 'Yes' : 'No'}
        />
      </CCol>
    </CRow>
  );
}
