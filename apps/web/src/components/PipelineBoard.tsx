'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useRouter } from 'next/navigation';
import {
  CBadge,
  CButton,
  CCard,
  CCardBody,
  CFormCheck,
  CFormSelect,
} from '@coreui/react-pro';
import type { Task, TaskStatus, AgentVendor } from '@/lib/types';
import { PRIORITY_COLORS, PRIORITY_LABELS } from '@/lib/types';

interface PipelineBoardProps {
  tasks: Task[];
}

// Lanes map DevServer's real lifecycle (not Devin's generic three). The
// terminal-success status the worker actually sets is ``test`` (see
// agent_runner._update_task_status), so SHIPPED keys off ``test``;
// ``retired`` is the hidden/archived state (toggle to reveal).
interface Lane {
  key: string;
  title: string;
  statuses: TaskStatus[];
  color: string;
  hint: string;
}

const LANES: Lane[] = [
  { key: 'queued', title: 'Queued', statuses: ['pending', 'queued'], color: 'info', hint: 'Waiting to run (by priority)' },
  { key: 'active', title: 'Active', statuses: ['running', 'verifying'], color: 'success', hint: 'Agent working' },
  { key: 'needs_you', title: 'Needs You', statuses: ['blocked'], color: 'dark', hint: 'Awaiting a human decision' },
  { key: 'shipped', title: 'Shipped', statuses: ['test'], color: 'info', hint: 'Verified — PR ready' },
  { key: 'stalled', title: 'Stalled', statuses: ['failed', 'cancelled'], color: 'danger', hint: 'Stopped without shipping' },
];

// Vendor badge colours mirror the analytics palette intent
// (anthropic=purple, google=blue, openai=green, glm=amber) mapped onto the
// CoreUI colour names available for CBadge.
const VENDOR_COLOR: Record<AgentVendor, string> = {
  anthropic: 'primary',
  google: 'info',
  openai: 'success',
  glm: 'warning',
};

function realityColor(score: number): string {
  if (score >= 70) return 'success';
  if (score >= 40) return 'warning';
  return 'danger';
}

function TaskCard({ task, onAction }: { task: Task; onAction: () => void }) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);

  const act = useCallback(
    async (path: string) => {
      setBusy(true);
      try {
        await fetch(path, { method: 'POST' });
        onAction();
      } finally {
        setBusy(false);
      }
    },
    [onAction],
  );

  const reality = task.reality_score;

  return (
    <CCard className="mb-2 shadow-sm" style={{ cursor: 'pointer' }}>
      <CCardBody className="p-2">
        <div
          onClick={() => router.push(`/tasks/${task.id}`)}
          role="button"
          tabIndex={0}
        >
          <div className="d-flex justify-content-between align-items-start mb-1">
            <strong className="small">{task.task_key}</strong>
            <CBadge color={PRIORITY_COLORS[task.priority]} shape="rounded-pill">
              {PRIORITY_LABELS[task.priority]}
            </CBadge>
          </div>
          <div className="small text-body-secondary mb-2" style={{ lineHeight: 1.25 }}>
            {task.title}
          </div>
          <div className="d-flex flex-wrap gap-1 mb-1">
            <CBadge color={VENDOR_COLOR[task.agent_vendor] ?? 'secondary'}>
              {task.agent_vendor}
            </CBadge>
            {task.repo_name && <CBadge color="light" className="text-dark">{task.repo_name}</CBadge>}
            {typeof reality === 'number' && reality !== null && (
              <CBadge color={realityColor(reality)} title="Reality score">
                R {reality}
              </CBadge>
            )}
          </div>
          {task.abstain_reason && (
            <div className="small text-danger mb-1" style={{ lineHeight: 1.2 }}>
              {task.abstain_reason}
            </div>
          )}
        </div>
        <div className="d-flex flex-wrap gap-1 mt-1">
          <CButton
            size="sm"
            color="outline-primary"
            onClick={() => router.push(`/tasks/${task.id}`)}
          >
            View
          </CButton>
          {task.repo_id && (
            <CButton
              size="sm"
              color="outline-secondary"
              onClick={() => router.push(`/repos`)}
              title="Repo knowledge"
            >
              Repo
            </CButton>
          )}
          {task.status === 'pending' && (
            <CButton size="sm" color="outline-success" disabled={busy} onClick={() => act(`/api/tasks/${task.id}/enqueue`)}>
              Enqueue
            </CButton>
          )}
          {['pending', 'queued', 'running', 'verifying', 'blocked'].includes(task.status) && (
            <CButton size="sm" color="outline-danger" disabled={busy} onClick={() => act(`/api/tasks/${task.id}/cancel`)}>
              Cancel
            </CButton>
          )}
          {['failed', 'blocked', 'cancelled'].includes(task.status) && (
            <CButton size="sm" color="outline-warning" disabled={busy} onClick={() => act(`/api/tasks/${task.id}/continue`)}>
              Continue
            </CButton>
          )}
        </div>
      </CCardBody>
    </CCard>
  );
}

export function PipelineBoard({ tasks: initialTasks }: PipelineBoardProps) {
  const [tasks, setTasks] = useState<Task[]>(initialTasks);
  const [showRetired, setShowRetired] = useState(false);
  const [repoFilter, setRepoFilter] = useState('');
  const [vendorFilter, setVendorFilter] = useState('');
  const [needsYouOnly, setNeedsYouOnly] = useState(false);
  const refetchTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const fetchTasks = useCallback(async () => {
    try {
      const res = await fetch('/api/tasks?limit=500');
      if (res.ok) setTasks(await res.json());
    } catch {
      // keep last-known state on transient failure
    }
  }, []);

  // Live updates: a default WS client receives every task_event (the server
  // subscribes new clients to taskId 0 = all). Not every status transition
  // emits ``status_change``, so we debounce-refetch the full list on ANY
  // event — correct regardless of which event fired. No polling otherwise.
  useEffect(() => {
    const wsUrl = process.env.NEXT_PUBLIC_WS_URL || 'ws://localhost:3200/api/ws';
    let ws: WebSocket | null = null;
    let reconnect: ReturnType<typeof setTimeout> | null = null;
    let closed = false;

    const scheduleRefetch = () => {
      if (refetchTimer.current) clearTimeout(refetchTimer.current);
      refetchTimer.current = setTimeout(fetchTasks, 600);
    };

    const connect = () => {
      ws = new WebSocket(wsUrl);
      ws.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data);
          if (msg.type === 'task_event') scheduleRefetch();
        } catch {
          // ignore malformed payloads
        }
      };
      ws.onclose = () => {
        if (!closed) reconnect = setTimeout(connect, 3000);
      };
    };

    connect();
    return () => {
      closed = true;
      if (reconnect) clearTimeout(reconnect);
      if (refetchTimer.current) clearTimeout(refetchTimer.current);
      ws?.close();
    };
  }, [fetchTasks]);

  const repos = useMemo(
    () => Array.from(new Set(tasks.map((t) => t.repo_name).filter(Boolean))).sort() as string[],
    [tasks],
  );

  const visible = useMemo(() => {
    return tasks.filter((t) => {
      if (!showRetired && t.status === 'retired') return false;
      if (repoFilter && t.repo_name !== repoFilter) return false;
      if (vendorFilter && t.agent_vendor !== vendorFilter) return false;
      if (needsYouOnly && t.status !== 'blocked') return false;
      return true;
    });
  }, [tasks, showRetired, repoFilter, vendorFilter, needsYouOnly]);

  const byLane = useMemo(() => {
    const map: Record<string, Task[]> = {};
    for (const lane of LANES) map[lane.key] = [];
    for (const t of visible) {
      const lane = LANES.find((l) => l.statuses.includes(t.status));
      if (lane) map[lane.key].push(t);
    }
    return map;
  }, [visible]);

  return (
    <>
      <div className="d-flex justify-content-end align-items-center mb-3 flex-wrap gap-2">
        <div className="d-flex align-items-center gap-2 flex-wrap">
          <CFormSelect
            size="sm"
            style={{ width: 'auto' }}
            value={repoFilter}
            onChange={(e) => setRepoFilter(e.target.value)}
          >
            <option value="">All repos</option>
            {repos.map((r) => (
              <option key={r} value={r}>{r}</option>
            ))}
          </CFormSelect>
          <CFormSelect
            size="sm"
            style={{ width: 'auto' }}
            value={vendorFilter}
            onChange={(e) => setVendorFilter(e.target.value)}
          >
            <option value="">All vendors</option>
            <option value="anthropic">Anthropic</option>
            <option value="google">Google</option>
            <option value="openai">OpenAI</option>
            <option value="glm">GLM</option>
          </CFormSelect>
          <CFormCheck
            id="needsYouOnly"
            label="Needs you only"
            checked={needsYouOnly}
            onChange={(e) => setNeedsYouOnly(e.target.checked)}
          />
          <CFormCheck
            id="boardShowRetired"
            label="Show retired"
            checked={showRetired}
            onChange={(e) => setShowRetired(e.target.checked)}
          />
        </div>
      </div>

      <div className="d-flex gap-3 align-items-start" style={{ overflowX: 'auto' }}>
        {LANES.map((lane) => (
          <div key={lane.key} style={{ minWidth: 260, flex: '1 1 0' }}>
            <div className="d-flex justify-content-between align-items-center mb-2">
              <span className="fw-semibold">{lane.title}</span>
              <CBadge color={lane.color} shape="rounded-pill">
                {byLane[lane.key].length}
              </CBadge>
            </div>
            <div className="text-body-secondary small mb-2">{lane.hint}</div>
            {byLane[lane.key].length === 0 ? (
              <div className="text-body-secondary small fst-italic">—</div>
            ) : (
              byLane[lane.key].map((task) => (
                <TaskCard key={task.id} task={task} onAction={fetchTasks} />
              ))
            )}
          </div>
        ))}
      </div>
    </>
  );
}
