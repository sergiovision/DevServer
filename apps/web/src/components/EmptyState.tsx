/**
 * EmptyState — the one way an empty list says so.
 *
 * Why it exists: every list used to improvise its own "No data" / "Nothing
 * yet" line with different markup, spacing and tone, and most never told the
 * operator what to do next. This component fixes the shape: a short title,
 * an optional hint explaining how the list gets filled, an optional icon and
 * an optional call-to-action. `compact` is for narrow panels and table cells
 * where a centred block would dominate the layout.
 *
 * Presentational only: no data fetching, no state.
 */
import type { ReactNode } from 'react';

interface EmptyStateProps {
  title: ReactNode;
  hint?: ReactNode;
  icon?: ReactNode;
  action?: ReactNode;
  /** Left-aligned, smaller padding — for side panels and lane columns. */
  compact?: boolean;
  className?: string;
}

export function EmptyState({ title, hint, icon, action, compact = false, className }: EmptyStateProps) {
  const layout = compact ? 'py-2' : 'p-4 text-center';
  return (
    <div className={`text-body-secondary ${layout}${className ? ` ${className}` : ''}`} role="status">
      {icon && <div className={compact ? 'mb-1' : 'mb-2 fs-3'} aria-hidden="true">{icon}</div>}
      <div className={compact ? 'small fw-semibold' : 'fw-semibold'}>{title}</div>
      {hint && <div className="small mt-1">{hint}</div>}
      {action && <div className="mt-2">{action}</div>}
    </div>
  );
}
