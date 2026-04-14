'use client';

import React, { useEffect, useState } from 'react';
import {
  CCard,
  CCardBody,
  CCardHeader,
  CRow,
  CCol,
  CFormSelect,
  CSpinner,
} from '@coreui/react-pro';
import {
  Chart as ChartJS,
  CategoryScale,
  LinearScale,
  BarElement,
  LineElement,
  PointElement,
  ArcElement,
  Title,
  Tooltip,
  Legend,
  Filler,
} from 'chart.js';
import { Line, Bar, Doughnut } from 'react-chartjs-2';

// Register Chart.js modules
ChartJS.register(
  CategoryScale, LinearScale, BarElement, LineElement,
  PointElement, ArcElement, Title, Tooltip, Legend, Filler,
);

// ── Types ───────────────────────────────────────────────────────────────────

interface DailyRow {
  date: string;
  completed: number;
  failed: number;
  cost_usd: string | number;
  total_duration_ms: string | number;
  total_turns: string | number;
}

interface VendorDailyRow {
  vendor: string;
  date: string;
  cost_usd: string | number;
  runs: string | number;
  successes: string | number;
  duration_ms: string | number;
  turns: string | number;
}

interface VendorTotal {
  vendor: string;
  cost_usd: string | number;
  runs: string | number;
  successes: string | number;
}

interface Totals {
  total_completed: string | number;
  total_failed: string | number;
  total_cost: string | number;
  total_duration_ms: string | number;
  total_turns: string | number;
}

interface AnalyticsData {
  days: number;
  daily: DailyRow[];
  vendor_daily: VendorDailyRow[];
  totals: Totals;
  vendor_totals: VendorTotal[];
}

// ── Helpers ─────────────────────────────────────────────────────────────────

const VENDOR_COLORS: Record<string, string> = {
  anthropic: 'rgba(139, 92, 246, 0.8)',   // purple
  google:    'rgba(66, 133, 244, 0.8)',    // blue
  openai:    'rgba(16, 163, 127, 0.8)',    // green
  glm:       'rgba(245, 158, 11, 0.8)',    // amber
};

const VENDOR_COLORS_BG: Record<string, string> = {
  anthropic: 'rgba(139, 92, 246, 0.15)',
  google:    'rgba(66, 133, 244, 0.15)',
  openai:    'rgba(16, 163, 127, 0.15)',
  glm:       'rgba(245, 158, 11, 0.15)',
};

function n(v: string | number): number {
  return typeof v === 'string' ? parseFloat(v) || 0 : v || 0;
}

function fmtDate(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

const CHART_OPTS = {
  responsive: true,
  maintainAspectRatio: false,
  plugins: {
    legend: { display: false },
  },
  scales: {
    x: {
      grid: { display: false },
      ticks: { maxTicksLimit: 10 },
    },
    y: {
      beginAtZero: true,
      grid: { color: 'rgba(128,128,128,0.1)' },
    },
  },
} as const;

function chartOpts(legendDisplay = false) {
  return {
    responsive: true,
    maintainAspectRatio: false,
    plugins: {
      legend: { display: legendDisplay, position: 'top' as const },
    },
    scales: {
      x: {
        grid: { display: false },
        ticks: { maxTicksLimit: 10 },
      },
      y: {
        beginAtZero: true,
        grid: { color: 'rgba(128,128,128,0.1)' },
      },
    },
  };
}

// ── Component ───────────────────────────────────────────────────────────────

export function DashboardCharts() {
  const [data, setData] = useState<AnalyticsData | null>(null);
  const [loading, setLoading] = useState(true);
  const [days, setDays] = useState(30);

  useEffect(() => {
    setLoading(true);
    fetch(`/api/analytics?days=${days}`)
      .then((r) => r.json())
      .then((d) => { setData(d); setLoading(false); })
      .catch(() => setLoading(false));
  }, [days]);

  if (loading) {
    return (
      <CCard className="mb-4">
        <CCardBody className="text-center py-5">
          <CSpinner color="primary" />
          <p className="mt-2 text-body-secondary">Loading analytics...</p>
        </CCardBody>
      </CCard>
    );
  }

  if (!data || data.daily.length === 0) {
    return (
      <CCard className="mb-4">
        <CCardBody className="text-center py-4 text-body-secondary">
          No analytics data yet. Complete some tasks to see charts.
        </CCardBody>
      </CCard>
    );
  }

  const labels = data.daily.map((r) => fmtDate(r.date));
  const vendors = [...new Set(data.vendor_daily.map((r) => r.vendor))].sort();

  // ── 1. Avg Duration (bar) ──────────────────────────────────────────────────
  const durationData = {
    labels,
    datasets: [{
      label: 'Avg Duration (min)',
      data: data.daily.map((r) => {
        const total = n(r.completed) + n(r.failed);
        if (total === 0) return 0;
        return Math.round(n(r.total_duration_ms) / total / 60000 * 10) / 10;
      }),
      backgroundColor: 'rgba(168, 85, 247, 0.6)',
      borderRadius: 3,
    }],
  };

  // ── 2. Turns per Task (line) ──────────────────────────────────────────────
  const turnsData = {
    labels,
    datasets: [{
      label: 'Avg Turns',
      data: data.daily.map((r) => {
        const total = n(r.completed) + n(r.failed);
        if (total === 0) return 0;
        return Math.round(n(r.total_turns) / total * 10) / 10;
      }),
      borderColor: 'rgba(245, 158, 11, 1)',
      backgroundColor: 'rgba(245, 158, 11, 0.1)',
      fill: true,
      tension: 0.3,
      pointRadius: 2,
    }],
  };

  // ── 3. Cost per Vendor (stacked line + doughnut) ──────────────────────────
  // Build per-vendor daily cost datasets
  const vendorCostDatasets = vendors.map((vendor) => {
    const vendorRows = data.vendor_daily.filter((r) => r.vendor === vendor);
    const byDate: Record<string, number> = {};
    vendorRows.forEach((r) => { byDate[fmtDate(r.date)] = n(r.cost_usd); });
    return {
      label: vendor,
      data: labels.map((l) => byDate[l] || 0),
      borderColor: VENDOR_COLORS[vendor] || 'rgba(128,128,128,0.8)',
      backgroundColor: VENDOR_COLORS_BG[vendor] || 'rgba(128,128,128,0.1)',
      fill: true,
      tension: 0.3,
      pointRadius: 1,
    };
  });

  const vendorCostLine = { labels, datasets: vendorCostDatasets };

  // Doughnut for vendor totals
  const vendorDoughnut = {
    labels: data.vendor_totals.map((v) => v.vendor),
    datasets: [{
      data: data.vendor_totals.map((v) => n(v.cost_usd)),
      backgroundColor: data.vendor_totals.map((v) => VENDOR_COLORS[v.vendor] || 'rgba(128,128,128,0.8)'),
      borderWidth: 0,
    }],
  };

  // ── Summary numbers ───────────────────────────────────────────────────────
  const totComp = n(data.totals.total_completed);
  const totFail = n(data.totals.total_failed);
  const totCost = n(data.totals.total_cost);
  const totDurH = Math.round(n(data.totals.total_duration_ms) / 3600000 * 10) / 10;
  const totTurns = n(data.totals.total_turns);
  const successRate = totComp + totFail > 0 ? Math.round(totComp / (totComp + totFail) * 100) : 0;

  return (
    <>
      {/* Period selector + summary row */}
      <CCard className="mb-4">
        <CCardHeader className="d-flex justify-content-between align-items-center">
          <strong>Analytics</strong>
          <CFormSelect
            style={{ width: 140 }}
            value={days}
            onChange={(e) => setDays(parseInt(e.target.value))}
          >
            <option value={7}>Last 7 days</option>
            <option value={14}>Last 14 days</option>
            <option value={30}>Last 30 days</option>
            <option value={60}>Last 60 days</option>
            <option value={90}>Last 90 days</option>
          </CFormSelect>
        </CCardHeader>
        <CCardBody>
          <CRow className="text-center">
            <CCol>
              <div className="fs-4 fw-bold text-success">{totComp}</div>
              <div className="text-body-secondary small">Completed</div>
            </CCol>
            <CCol>
              <div className="fs-4 fw-bold text-danger">{totFail}</div>
              <div className="text-body-secondary small">Failed</div>
            </CCol>
            <CCol>
              <div className="fs-4 fw-bold text-primary">{successRate}%</div>
              <div className="text-body-secondary small">Success Rate</div>
            </CCol>
            <CCol>
              <div className="fs-4 fw-bold">${totCost.toFixed(2)}</div>
              <div className="text-body-secondary small">Total Cost</div>
            </CCol>
            <CCol>
              <div className="fs-4 fw-bold">{totDurH}h</div>
              <div className="text-body-secondary small">Agent Time</div>
            </CCol>
            <CCol>
              <div className="fs-4 fw-bold">{totTurns}</div>
              <div className="text-body-secondary small">Total Turns</div>
            </CCol>
          </CRow>
        </CCardBody>
      </CCard>

      {/* Charts row 1: Duration + Turns */}
      <CRow className="mb-4">
        <CCol lg={6}>
          <CCard className="h-100">
            <CCardHeader><strong>Avg Duration per Task</strong></CCardHeader>
            <CCardBody style={{ height: 280 }}>
              <Bar data={durationData} options={chartOpts()} />
            </CCardBody>
          </CCard>
        </CCol>
        <CCol lg={6}>
          <CCard className="h-100">
            <CCardHeader><strong>Avg Turns per Task</strong></CCardHeader>
            <CCardBody style={{ height: 280 }}>
              <Line data={turnsData} options={chartOpts()} />
            </CCardBody>
          </CCard>
        </CCol>
      </CRow>

      {/* Charts row 3: Cost per Vendor */}
      <CRow className="mb-4">
        <CCol lg={8}>
          <CCard className="h-100">
            <CCardHeader><strong>Cost by Vendor</strong></CCardHeader>
            <CCardBody style={{ height: 280 }}>
              <Line data={vendorCostLine} options={chartOpts(true)} />
            </CCardBody>
          </CCard>
        </CCol>
        <CCol lg={4}>
          <CCard className="h-100">
            <CCardHeader><strong>Vendor Cost Share</strong></CCardHeader>
            <CCardBody className="d-flex align-items-center justify-content-center" style={{ height: 280 }}>
              {data.vendor_totals.length > 0 ? (
                <Doughnut
                  data={vendorDoughnut}
                  options={{
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                      legend: { display: true, position: 'bottom' },
                    },
                  }}
                />
              ) : (
                <span className="text-body-secondary">No vendor data</span>
              )}
            </CCardBody>
          </CCard>
        </CCol>
      </CRow>
    </>
  );
}
