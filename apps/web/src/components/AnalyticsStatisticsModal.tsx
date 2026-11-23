'use client';

/**
 * AnalyticsStatisticsModal — the exact numbers behind the dashboard charts,
 * as tables: per-vendor runs / cost, output tokens per second, and average
 * tokens per task (input vs output).
 *
 * Why it exists: charts are for shape, not for reading a value to the token;
 * the tables also serve as the accessible, colour-free view of the same data.
 * Presentational only — the dashboard passes in what `/api/analytics`
 * already returned, so opening it costs no extra request.
 */
import {
  CModal,
  CModalBody,
  CModalHeader,
  CModalTitle,
  CTable,
  CTableBody,
  CTableDataCell,
  CTableHead,
  CTableHeaderCell,
  CTableRow,
} from '@coreui/react-pro';
import {
  OVERALL_VENDOR,
  toFiniteNumber,
  type SqlNumeric,
  type TaskTokenAverages,
  type TokensPerTask,
  type VendorThroughput,
} from '@/lib/analytics-metrics';
import { EmptyState } from './EmptyState';

export interface VendorCostTotal {
  vendor: string;
  cost_usd: SqlNumeric;
  runs: SqlNumeric;
  successes: SqlNumeric;
}

interface AnalyticsStatisticsModalProps {
  visible: boolean;
  onClose: () => void;
  days: number;
  vendorTotals: readonly VendorCostTotal[];
  throughput: readonly VendorThroughput[];
  tokensPerTask: TokensPerTask | null;
}

const integerFormat = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });
const rateFormat = new Intl.NumberFormat(undefined, { minimumFractionDigits: 1, maximumFractionDigits: 1 });

function formatCount(value: number | null): string {
  return value === null ? '—' : integerFormat.format(value);
}

function formatRate(value: number | null): string {
  return value === null ? '—' : rateFormat.format(value);
}

function TokensRow({ row, emphasise }: { row: TaskTokenAverages; emphasise?: boolean }) {
  const className = emphasise ? 'fw-semibold' : undefined;
  return (
    <CTableRow className={className}>
      <CTableDataCell>{row.vendor === OVERALL_VENDOR ? 'All vendors' : row.vendor}</CTableDataCell>
      <CTableDataCell className="text-end">{formatCount(row.tasks)}</CTableDataCell>
      <CTableDataCell className="text-end">{formatCount(row.avg_input_tokens)}</CTableDataCell>
      <CTableDataCell className="text-end">{formatCount(row.avg_cache_read_tokens)}</CTableDataCell>
      <CTableDataCell className="text-end">{formatCount(row.avg_output_tokens)}</CTableDataCell>
    </CTableRow>
  );
}

export function AnalyticsStatisticsModal({
  visible,
  onClose,
  days,
  vendorTotals,
  throughput,
  tokensPerTask,
}: AnalyticsStatisticsModalProps) {
  const hasTokens = throughput.length > 0;
  return (
    <CModal visible={visible} onClose={onClose} size="lg" scrollable aria-labelledby="analytics-statistics-title">
      <CModalHeader>
        <CModalTitle id="analytics-statistics-title">Statistics — last {days} day{days === 1 ? '' : 's'}</CModalTitle>
      </CModalHeader>
      <CModalBody>
        <h6>Runs and cost by vendor</h6>
        {vendorTotals.length === 0 ? (
          <EmptyState compact title="No runs in this period" />
        ) : (
          <CTable small hover responsive className="mb-4">
            <CTableHead>
              <CTableRow>
                <CTableHeaderCell>Vendor</CTableHeaderCell>
                <CTableHeaderCell className="text-end">Runs</CTableHeaderCell>
                <CTableHeaderCell className="text-end">Successes</CTableHeaderCell>
                <CTableHeaderCell className="text-end">Cost (USD)</CTableHeaderCell>
              </CTableRow>
            </CTableHead>
            <CTableBody>
              {vendorTotals.map((row) => (
                <CTableRow key={row.vendor}>
                  <CTableDataCell>{row.vendor}</CTableDataCell>
                  <CTableDataCell className="text-end">{formatCount(toFiniteNumber(row.runs))}</CTableDataCell>
                  <CTableDataCell className="text-end">{formatCount(toFiniteNumber(row.successes))}</CTableDataCell>
                  <CTableDataCell className="text-end">${toFiniteNumber(row.cost_usd).toFixed(2)}</CTableDataCell>
                </CTableRow>
              ))}
            </CTableBody>
          </CTable>
        )}

        {!hasTokens ? (
          <EmptyState
            title="No token data in this period"
            hint="Token metrics appear once the worker reports token counts with each attempt."
          />
        ) : (
          <>
            <h6>Output throughput by vendor</h6>
            <CTable small hover responsive className="mb-4">
              <CTableHead>
                <CTableRow>
                  <CTableHeaderCell>Vendor</CTableHeaderCell>
                  <CTableHeaderCell className="text-end">Attempts</CTableHeaderCell>
                  <CTableHeaderCell className="text-end">Output tokens</CTableHeaderCell>
                  <CTableHeaderCell className="text-end">Agent-active s</CTableHeaderCell>
                  <CTableHeaderCell className="text-end">Tokens / s</CTableHeaderCell>
                </CTableRow>
              </CTableHead>
              <CTableBody>
                {throughput.map((row) => (
                  <CTableRow key={row.vendor}>
                    <CTableDataCell>{row.vendor}</CTableDataCell>
                    <CTableDataCell className="text-end">{formatCount(row.runs)}</CTableDataCell>
                    <CTableDataCell className="text-end">{formatCount(row.output_tokens)}</CTableDataCell>
                    <CTableDataCell className="text-end">{formatRate(row.active_seconds)}</CTableDataCell>
                    <CTableDataCell className="text-end">{formatRate(row.output_tokens_per_second)}</CTableDataCell>
                  </CTableRow>
                ))}
              </CTableBody>
            </CTable>

            {tokensPerTask && (
              <>
                <h6>Average tokens per task</h6>
                <p className="small text-body-secondary mb-2">
                  Input includes cache reads and cache writes (prompt context the model consumed).
                </p>
                <CTable small hover responsive className="mb-0">
                  <CTableHead>
                    <CTableRow>
                      <CTableHeaderCell>Vendor</CTableHeaderCell>
                      <CTableHeaderCell className="text-end">Tasks</CTableHeaderCell>
                      <CTableHeaderCell className="text-end">Avg input</CTableHeaderCell>
                      <CTableHeaderCell className="text-end">of which cache reads</CTableHeaderCell>
                      <CTableHeaderCell className="text-end">Avg output</CTableHeaderCell>
                    </CTableRow>
                  </CTableHead>
                  <CTableBody>
                    {tokensPerTask.by_vendor.map((row) => (
                      <TokensRow key={row.vendor} row={row} />
                    ))}
                    <TokensRow row={tokensPerTask.overall} emphasise />
                  </CTableBody>
                </CTable>
              </>
            )}
          </>
        )}
      </CModalBody>
    </CModal>
  );
}
