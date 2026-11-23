'use client';

/**
 * TasksView — the `/tasks` page shell: toolbar (search, view toggle, grid
 * options) over either the grid (TaskTable) or the board (PipelineBoard).
 *
 * URL contract: `?q=` seeds the search box and `?status=a,b` restricts the
 * list to those statuses (the sidebar's "Needs You" link is
 * `/tasks?status=blocked`). Typing in the search box rewrites `?q=` with
 * `history.replaceState` rather than the router, because this page is
 * server-rendered and a router navigation would refetch every task per
 * keystroke. Matching rules live in the pure `lib/task-search.ts`.
 */
import { useCallback, useMemo, useState } from 'react';
import Link from 'next/link';
import { useRouter, useSearchParams } from 'next/navigation';
import { CFormCheck, CButtonGroup, CButton, CBadge, CFormInput, CCloseButton } from '@coreui/react-pro';
import { TaskTable } from '@/components/TaskTable';
import { PipelineBoard } from '@/components/PipelineBoard';
import { ImportModal } from '@/components/ImportModal';
import { EmptyState } from '@/components/EmptyState';
import { parseSearchTerms, parseStatusFilter, taskMatchesStatus, taskMatchesTerms } from '@/lib/task-search';
import type { Task } from '@/lib/types';
import { api } from '@/lib/api-client';

interface TasksViewProps {
  tasks: Task[];
}

type ViewMode = 'grid' | 'board';

/** Rewrite one query parameter in place without a Next navigation. */
function replaceQueryParam(name: string, value: string) {
  const url = new URL(window.location.href);
  if (value) url.searchParams.set(name, value);
  else url.searchParams.delete(name);
  window.history.replaceState(window.history.state, '', url);
}

export function TasksView({ tasks }: TasksViewProps) {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [view, setView] = useState<ViewMode>('grid');
  const [searchQuery, setSearchQuery] = useState(() => searchParams.get('q') ?? '');
  const statusParam = searchParams.get('status');
  const statusFilter = useMemo(() => parseStatusFilter(statusParam), [statusParam]);
  const searchTerms = useMemo(() => parseSearchTerms(searchQuery), [searchQuery]);
  const [showRetired, setShowRetired] = useState(false);
  const [groupByRepo, setGroupByRepo] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set());
  const [retiring, setRetiring] = useState(false);

  // A sidebar link to a different `?q=` re-renders this view with new
  // params; keep the box in step with them (adjust-state-during-render
  // pattern, so there is no extra effect pass).
  const queryParam = searchParams.get('q') ?? '';
  const [syncedQueryParam, setSyncedQueryParam] = useState(queryParam);
  if (queryParam !== syncedQueryParam) {
    setSyncedQueryParam(queryParam);
    setSearchQuery(queryParam);
  }

  const handleSearchChange = useCallback((value: string) => {
    setSearchQuery(value);
    // Written untrimmed: Next syncs replaceState into useSearchParams, and the
    // effect above would otherwise eat a trailing space mid-typing.
    replaceQueryParam('q', value);
  }, []);

  const clearStatusFilter = useCallback(() => {
    const params = new URLSearchParams(searchParams.toString());
    params.delete('status');
    const query = params.toString();
    router.replace(query ? `/tasks?${query}` : '/tasks');
  }, [router, searchParams]);

  const visibleTasks = useMemo(
    () => tasks.filter((task) => taskMatchesStatus(task, statusFilter) && taskMatchesTerms(task, searchTerms)),
    [tasks, statusFilter, searchTerms],
  );
  // Asking for retired tasks by URL must show them even with the toggle off.
  const effectiveShowRetired = showRetired || statusFilter.includes('retired');
  const filtersActive = statusFilter.length > 0 || searchTerms.length > 0;

  // The grid may render as one flat table or several per-repo tables. Each
  // table reports the full selection *for its own rows* — replace that
  // slice of the global set so selection survives across groups.
  const replaceGroupSelection = useCallback((groupTasks: Task[], selected: Task[]) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      for (const t of groupTasks) next.delete(t.id);
      for (const t of selected) next.add(t.id);
      return next;
    });
  }, []);

  const handleRetire = useCallback(async () => {
    // Only retire rows the operator can currently see: a selection made
    // before narrowing the search must not act on hidden tasks.
    const visibleIds = new Set(visibleTasks.map((task) => task.id));
    const ids = Array.from(selectedIds).filter((id) => visibleIds.has(id));
    if (ids.length === 0) return;
    setRetiring(true);
    try {
      await Promise.all(
        ids.map((id) =>
          // One failed row must not stop the others; the refresh shows what stuck.
          api.patch(`/api/tasks/${id}`, { status: 'retired' }).catch(() => undefined),
        ),
      );
      setSelectedIds(new Set());
      router.refresh();
    } finally {
      setRetiring(false);
    }
  }, [selectedIds, visibleTasks, router]);

  return (
    <>
      <div className="d-flex justify-content-between align-items-center mb-4 flex-wrap gap-3">
        <div className="d-flex align-items-center gap-3 flex-wrap">
          <Link href="/tasks/new" className="btn btn-primary">
            + New Task
          </Link>
          <CButton color="secondary" variant="outline" onClick={() => setImportOpen(true)}>
            Import
          </CButton>
          {view === 'grid' && (
            <CButton
              color="dark"
              variant="outline"
              disabled={selectedIds.size === 0 || retiring}
              onClick={handleRetire}
            >
              {retiring ? 'Retiring…' : `Retire${selectedIds.size ? ` (${selectedIds.size})` : ''}`}
            </CButton>
          )}
          <CButtonGroup role="group" aria-label="Task view">
            <CButton
              color="secondary"
              variant={view === 'grid' ? undefined : 'outline'}
              size="sm"
              onClick={() => setView('grid')}
            >
              Grid
            </CButton>
            <CButton
              color="secondary"
              variant={view === 'board' ? undefined : 'outline'}
              size="sm"
              onClick={() => setView('board')}
            >
              Board
            </CButton>
          </CButtonGroup>
          {/* Grid-only controls; the board carries its own filters/toggles. */}
          {view === 'grid' && (
            <>
              <CFormCheck
                id="showRetired"
                label="Show Retired"
                checked={showRetired}
                onChange={(e) => setShowRetired(e.target.checked)}
              />
              <CFormCheck
                id="groupByRepo"
                label="Group by Repository"
                checked={groupByRepo}
                onChange={(e) => setGroupByRepo(e.target.checked)}
              />
            </>
          )}
          <CFormInput
            type="search"
            size="sm"
            style={{ width: 240 }}
            placeholder="Search key, title, repo, status…"
            aria-label="Search tasks"
            value={searchQuery}
            onChange={(e) => handleSearchChange(e.target.value)}
          />
          {statusFilter.length > 0 && (
            <CBadge color="dark" className="d-inline-flex align-items-center gap-2 py-1">
              Status: {statusFilter.join(', ')}
              <CCloseButton white aria-label="Clear status filter" onClick={clearStatusFilter} />
            </CBadge>
          )}
        </div>
        <h2 className="mb-0">Tasks</h2>
      </div>

      {view === 'grid' && visibleTasks.length === 0 ? (
        filtersActive ? (
          <EmptyState
            title="No tasks match"
            hint="Every search term must match the key, title, repo, status, vendor, model, type or a label."
            action={
              <CButton
                size="sm"
                color="secondary"
                variant="outline"
                onClick={() => {
                  handleSearchChange('');
                  if (statusFilter.length > 0) clearStatusFilter();
                }}
              >
                Clear filters
              </CButton>
            }
          />
        ) : (
          <EmptyState
            title="No tasks yet"
            hint="Create a task, or import some from an external source."
            action={
              <Link href="/tasks/new" className="btn btn-sm btn-primary">
                + New Task
              </Link>
            }
          />
        )
      ) : view === 'grid' ? (
        <TaskTable
          tasks={visibleTasks}
          showRetired={effectiveShowRetired}
          groupByRepo={groupByRepo}
          selectedIds={selectedIds}
          onSelectionChange={replaceGroupSelection}
        />
      ) : (
        <PipelineBoard tasks={tasks} searchTerms={searchTerms} statusFilter={statusFilter} />
      )}

      <ImportModal
        visible={importOpen}
        onClose={() => setImportOpen(false)}
        defaultTarget="task"
        onImported={() => router.refresh()}
      />
    </>
  );
}
