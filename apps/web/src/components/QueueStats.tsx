'use client';

import React, { useState, useEffect } from 'react';
import { CRow, CCol, CWidgetStatsF } from '@coreui/react-pro';
import CIcon from '@coreui/icons-react';
import { cilMediaPlay, cilClock, cilCheckAlt, cilXCircle, cilMediaPause } from '@coreui/icons';
import type { QueueStatsResponse } from '@/lib/types';

export function QueueStats() {
  const [stats, setStats] = useState<QueueStatsResponse | null>(null);

  useEffect(() => {
    async function fetchStats() {
      try {
        const res = await fetch('/api/queue');
        if (res.ok) {
          setStats(await res.json());
        }
      } catch {
        // ignore
      }
    }
    fetchStats();
    const interval = setInterval(fetchStats, 5000);
    return () => clearInterval(interval);
  }, []);

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
