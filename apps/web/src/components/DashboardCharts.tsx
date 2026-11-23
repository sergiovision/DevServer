'use client';

/**
 * DashboardCharts — the analytics block on the dashboard: summary tiles,
 * duration / turns / cost charts, token throughput and tokens-per-task
 * charts, and a Statistics button that opens the same data as tables
 * (AnalyticsStatisticsModal).
 *
 * All token math (rates, averages, zero-division) is done server-side by
 * `lib/analytics-metrics.ts`; this component only draws the numbers it gets.
 */
import React, { useState } from 'react';
import {
  CButton,
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
import { OVERALL_VENDOR, type TokensPerTask, type VendorThroughput } from '@/lib/analytics-metrics';
import { AnalyticsStatisticsModal } from './AnalyticsStatisticsModal';
import { EmptyState } from './EmptyState';
import { api } from '@/lib/api-client';
import { useResource } from '@/lib/use-async';

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

interface Governance {
  pr_count: string | number;
  cost_per_pr: string | number;
  abstain_count: string | number;
  abstain_savings_usd: string | number;
}

interface AnalyticsData {
  days: number;
  daily: DailyRow[];
  vendor_daily: VendorDailyRow[];
  totals: Totals;
  vendor_totals: VendorTotal[];
  governance?: Governance;
  // Token metrics; absent from an older server, empty until the worker
  // reports token counts.
  token_data_available?: boolean;
  token_throughput?: VendorThroughput[];
  tokens_per_task?: TokensPerTask;
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
  const [days, setDays] = useState(7);
  const [statisticsOpen, setStatisticsOpen] = useState(false);

  // A failed load keeps whatever was shown before (or the empty state).
  const { data, loading } = useResource<AnalyticsData | null>(
    (signal) => api.get<AnalyticsData | null>(`/api/analytics?days=${days}`, { signal }),
    [days],
  );

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
        <CCardBody>
          <EmptyState title="No analytics data yet" hint="Complete some tasks to see charts." />
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

  // ── 4. Token throughput + tokens per task ─────────────────────────────────
  const throughput = data.token_throughput ?? [];
  const tokensPerTask = data.tokens_per_task ?? null;
  const hasTokenData = (data.token_data_available ?? false) && throughput.length > 0;

  // One bar per vendor, coloured by the vendor (same identity colours as the
  // cost charts). A vendor with no active seconds has a null rate: Chart.js
  // leaves a gap rather than drawing a misleading zero.
  const throughputData = {
    labels: throughput.map((row) => row.vendor),
    datasets: [{
      label: 'Output tokens / s',
      data: throughput.map((row) => row.output_tokens_per_second),
      backgroundColor: throughput.map((row) => VENDOR_COLORS[row.vendor] || 'rgba(128,128,128,0.8)'),
      borderRadius: 4,
      maxBarThickness: 48,
    }],
  };

  const tokenRows = tokensPerTask ? [...tokensPerTask.by_vendor, tokensPerTask.overall] : [];
  const tokensPerTaskData = {
    labels: tokenRows.map((row) => (row.vendor === OVERALL_VENDOR ? 'All vendors' : row.vendor)),
    datasets: [
      {
        label: 'Avg input tokens (incl. cache)',
        data: tokenRows.map((row) => row.avg_input_tokens),
        backgroundColor: 'rgba(66, 133, 244, 0.7)',
        borderRadius: 4,
        yAxisID: 'input',
      },
      {
        label: 'Avg output tokens',
        data: tokenRows.map((row) => row.avg_output_tokens),
        backgroundColor: 'rgba(245, 158, 11, 0.8)',
        borderRadius: 4,
        yAxisID: 'output',
      },
    ],
  };
  // Two y-axes on purpose: input (prompt + cache) is typically 10-100x the
  // output, so on one shared axis every output bar would flatten to zero and
  // the chart could not show output at all. Each axis is titled so the two
  // scales are never read against each other; the Statistics table carries
  // the exact numbers.
  const tokensPerTaskOptions = {
    responsive: true,
    maintainAspectRatio: false,
    plugins: { legend: { display: true, position: 'top' as const } },
    scales: {
      x: { grid: { display: false } },
      input: {
        type: 'linear' as const,
        position: 'left' as const,
        beginAtZero: true,
        title: { display: true, text: 'Input tokens' },
        grid: { color: 'rgba(128,128,128,0.1)' },
      },
      output: {
        type: 'linear' as const,
        position: 'right' as const,
        beginAtZero: true,
        title: { display: true, text: 'Output tokens' },
        grid: { drawOnChartArea: false },
      },
    },
  };


  return (
    <>
      {/* Period selector + summary row */}
      <CCard className="mb-4">
        <CCardHeader className="d-flex justify-content-between align-items-center">
          <strong>Analytics</strong>
          <div className="d-flex align-items-center gap-2">
            <CButton color="secondary" variant="outline" size="sm" onClick={() => setStatisticsOpen(true)}>
              Statistics
            </CButton>
            <CFormSelect
              style={{ width: 140 }}
              value={days}
              onChange={(e) => setDays(parseInt(e.target.value))}
            >
              <option value={1}>Last 1 day</option>
              <option value={7}>Last 7 days</option>
              <option value={14}>Last 14 days</option>
              <option value={30}>Last 30 days</option>
              <option value={60}>Last 60 days</option>
              <option value={90}>Last 90 days</option>
            </CFormSelect>
          </div>
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
                <EmptyState compact title="No vendor data" />
              )}
            </CCardBody>
          </CCard>
        </CCol>
      </CRow>

      {/* Charts row 4: token throughput + tokens per task */}
      <CRow className="mb-4">
        {hasTokenData ? (
          <>
            <CCol lg={5}>
              <CCard className="h-100">
                <CCardHeader><strong>Output Tokens / s by Vendor</strong></CCardHeader>
                <CCardBody style={{ height: 280 }}>
                  <Bar data={throughputData} options={chartOpts()} />
                </CCardBody>
              </CCard>
            </CCol>
            <CCol lg={7}>
              <CCard className="h-100">
                <CCardHeader><strong>Avg Tokens per Task: Input vs Output</strong></CCardHeader>
                <CCardBody style={{ height: 280 }}>
                  <Bar data={tokensPerTaskData} options={tokensPerTaskOptions} />
                </CCardBody>
              </CCard>
            </CCol>
          </>
        ) : (
          <CCol>
            <CCard>
              <CCardHeader><strong>Token Usage</strong></CCardHeader>
              <CCardBody>
                <EmptyState
                  compact
                  title="No token data in this period"
                  hint="Throughput and tokens-per-task charts appear once the worker reports token counts with each attempt."
                />
              </CCardBody>
            </CCard>
          </CCol>
        )}
      </CRow>

      <AnalyticsStatisticsModal
        visible={statisticsOpen}
        onClose={() => setStatisticsOpen(false)}
        days={days}
        vendorTotals={data.vendor_totals}
        throughput={throughput}
        tokensPerTask={tokensPerTask}
      />
    </>
  );
}
