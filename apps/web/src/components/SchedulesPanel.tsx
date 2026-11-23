'use client';

import { useCallback, useEffect, useState } from 'react';
import {
  CCard,
  CCardHeader,
  CCardBody,
  CButton,
  CBadge,
  CFormInput,
  CFormSelect,
  CListGroup,
  CListGroupItem,
} from '@coreui/react-pro';
import { EmptyState } from '@/components/EmptyState';
import { api, errorMessage, isApiError } from '@/lib/api-client';

interface Schedule {
  id: number;
  name: string;
  cron_expr: string;
  task_id: number | null;
  task_key: string | null;
  task_title: string | null;
  task_status: string | null;
  enabled: boolean;
  last_run_at: string | null;
  next_run_at: string | null;
}

interface Skill {
  id: number;
  name: string;
  domain: string | null;
}

interface TaskOption {
  id: number;
  task_key: string;
  title: string;
  status: string;
}

/**
 * `.catch` handler for list reads: an HTTP error yields `null` (the list keeps
 * its previous contents, as when the old code skipped `!res.ok`), while an
 * unreachable server still rejects the load.
 */
function ignoreHttpError(err: unknown): null {
  if (isApiError(err) && err.isNetworkError) throw err;
  return null;
}

function fmt(iso: string | null): string {
  if (!iso) return '—';
  const d = new Date(iso);
  return isNaN(d.getTime()) ? '—' : d.toLocaleString();
}

/** Schedule CRUD + Skills sync. A schedule re-runs an existing task on a
 *  cron subset: @hourly, @daily, every Nm/Nh, HH:MM. */
export function SchedulesPanel() {
  const [schedules, setSchedules] = useState<Schedule[]>([]);
  const [skills, setSkills] = useState<Skill[]>([]);
  const [tasks, setTasks] = useState<TaskOption[]>([]);
  const [name, setName] = useState('');
  const [cron, setCron] = useState('@daily');
  const [taskId, setTaskId] = useState('');
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    // A failing list leaves its previous contents in place; only an
    // unreachable server rejects the whole load.
    const [scheduleList, skillList, taskList] = await Promise.all([
      api.get<{ schedules?: Schedule[] } | null>('/api/schedules', { cache: 'no-store' }).catch(ignoreHttpError),
      api.get<{ skills?: Skill[] } | null>('/api/skills', { cache: 'no-store' }).catch(ignoreHttpError),
      api.get<TaskOption[] | null>('/api/tasks', { cache: 'no-store' }).catch(ignoreHttpError),
    ]);
    if (scheduleList) setSchedules(scheduleList.schedules ?? []);
    if (skillList) setSkills(skillList.skills ?? []);
    if (taskList) setTasks(taskList);
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const create = useCallback(async () => {
    if (!name.trim() || !taskId) return;
    setBusy(true);
    try {
      try {
        await api.post('/api/schedules', {
          name: name.trim(),
          cron_expr: cron.trim() || '@daily',
          task_id: parseInt(taskId, 10),
        });
      } catch (err) {
        alert(`Create failed: ${errorMessage(err)}`);
        return;
      }
      setName('');
      setTaskId('');
      await load();
    } finally {
      setBusy(false);
    }
  }, [name, cron, taskId, load]);

  const toggle = useCallback(
    async (s: Schedule) => {
      // Fire-and-forget: the reload shows the real state either way.
      await api.patch(`/api/schedules/${s.id}`, { enabled: !s.enabled }).catch(() => undefined);
      await load();
    },
    [load],
  );

  const runNow = useCallback(
    async (s: Schedule) => {
      try {
        await api.post(`/api/schedules/${s.id}/run`);
      } catch (err) {
        alert(`Run failed: ${errorMessage(err)}`);
      }
      await load();
    },
    [load],
  );

  const remove = useCallback(
    async (s: Schedule) => {
      if (!window.confirm(`Delete schedule "${s.name}"?`)) return;
      await api.delete(`/api/schedules/${s.id}`).catch(() => undefined);
      await load();
    },
    [load],
  );

  const syncSkills = useCallback(async () => {
    try {
      const d = await api.post<{ count?: number } | null>('/api/skills');
      alert(`Synced ${d?.count ?? 0} skills.`);
    } catch (err) {
      alert(`Sync failed: ${errorMessage(err)}`);
    }
    await load();
  }, [load]);

  return (
    <CCard className="mb-4">
      <CCardHeader className="d-flex justify-content-between align-items-center">
        <strong>Schedules ({schedules.length})</strong>
        <CButton size="sm" color="outline-secondary" onClick={syncSkills}>
          Sync skills ({skills.length})
        </CButton>
      </CCardHeader>
      <CCardBody>
        {/* Create row */}
        <div className="d-flex gap-2 mb-3 flex-wrap align-items-end">
          <div style={{ minWidth: 160 }}>
            <label className="form-label small mb-1">Name</label>
            <CFormInput size="sm" value={name} onChange={(e) => setName(e.target.value)} placeholder="Nightly re-run" />
          </div>
          <div style={{ width: 130 }}>
            <label className="form-label small mb-1">Cron</label>
            <CFormInput size="sm" value={cron} onChange={(e) => setCron(e.target.value)} placeholder="@daily" />
          </div>
          <div style={{ minWidth: 260 }}>
            <label className="form-label small mb-1">Task</label>
            <CFormSelect size="sm" value={taskId} onChange={(e) => setTaskId(e.target.value)}>
              <option value="">Select a task…</option>
              {tasks.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.task_key} — {t.title}
                </option>
              ))}
            </CFormSelect>
          </div>
          <CButton size="sm" color="primary" disabled={busy || !name.trim() || !taskId} onClick={create}>
            + Add
          </CButton>
        </div>

        {schedules.length === 0 ? (
          <EmptyState
            compact
            title="No schedules yet"
            hint="A schedule re-runs an existing task on a cron — the task is reset to pending and enqueued on every fire."
          />
        ) : (
          <CListGroup>
            {schedules.map((s) => (
              <CListGroupItem key={s.id} className="d-flex justify-content-between align-items-center gap-2">
                <div className="flex-grow-1">
                  <div className="d-flex align-items-center gap-2">
                    <strong>{s.name}</strong>
                    <CBadge color="info">{s.cron_expr}</CBadge>
                    <CBadge color={s.enabled ? 'success' : 'secondary'}>
                      {s.enabled ? 'enabled' : 'disabled'}
                    </CBadge>
                    {s.task_key ? (
                      <CBadge color="light" className="text-dark" title={s.task_title ?? ''}>
                        {s.task_key} ({s.task_status})
                      </CBadge>
                    ) : (
                      <CBadge color="danger">no task</CBadge>
                    )}
                  </div>
                  <div className="small text-body-secondary">
                    last {fmt(s.last_run_at)} · next {fmt(s.next_run_at)}
                  </div>
                </div>
                <div className="d-flex gap-1 flex-shrink-0">
                  <CButton size="sm" color="outline-success" onClick={() => runNow(s)}>Run</CButton>
                  <CButton size="sm" color="outline-secondary" onClick={() => toggle(s)}>
                    {s.enabled ? 'Disable' : 'Enable'}
                  </CButton>
                  <CButton size="sm" color="outline-danger" onClick={() => remove(s)}>Delete</CButton>
                </div>
              </CListGroupItem>
            ))}
          </CListGroup>
        )}
      </CCardBody>
    </CCard>
  );
}
