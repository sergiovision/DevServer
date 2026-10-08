/**
 * Typed adapters for CoreUI's ``CSmartTable``.
 *
 * Since @coreui/react-pro 5.29 the table types every row as ``Item``
 * (``{ [key: string]: unknown }``), which our row interfaces (``Task``,
 * ``Job``) and their typed scoped-column renderers no longer satisfy. These
 * helpers keep the call sites typed against the real row shape and do the
 * single cast to CoreUI's loose row type in one place.
 */

import type { ComponentProps, ReactNode } from 'react';
import type { CSmartTable } from '@coreui/react-pro';

type SmartTableProps = ComponentProps<typeof CSmartTable>;
type TableItems = NonNullable<SmartTableProps['items']>;

/** Pass typed rows to ``items`` / ``selected``. */
export function asTableItems<T extends object>(rows: T[]): TableItems {
  return rows as unknown as TableItems;
}

/** Read typed rows back out of ``onSelectedItemsChange``. */
export function fromTableItems<T extends object>(items: TableItems): T[] {
  return items as unknown as T[];
}

/** Pass typed per-column renderers to ``scopedColumns``. */
export function asScopedColumns<T extends object>(
  columns: Record<string, ((item: T, index: number) => ReactNode) | undefined>,
): SmartTableProps['scopedColumns'] {
  return columns as unknown as SmartTableProps['scopedColumns'];
}
