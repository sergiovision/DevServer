'use client';

/**
 * Picker for a task's (or template's) post-completion action: what to start
 * when this task finishes — another task, or a skill in the same repo — and on
 * which outcome.
 *
 * Controlled and stateless: it edits a `PostCompletionDraft` (see
 * `lib/post-completion-action.ts`) and the owning form converts it with
 * `draftToAction` on save, so a half-filled picker is reported as a
 * validation error instead of being stored. Task and skill lists are only
 * fetched once the matching kind is chosen, so the default "None" costs no
 * requests.
 *
 * `PostCompletionActionCard` wraps the picker with its own save for the task
 * detail sidebar.
 */

import { useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import {
  CAlert,
  CButton,
  CCard,
  CCardBody,
  CCardHeader,
  CCol,
  CFormInput,
  CFormLabel,
  CFormSelect,
  CRow,
} from '@coreui/react-pro';
import { api, errorMessage } from '@/lib/api-client';
import { useAsyncAction, useResource } from '@/lib/use-async';
import type { Task } from '@/lib/types';
import {
  POST_COMPLETION_TRIGGERS,
  POST_COMPLETION_TRIGGER_LABELS,
  actionToDraft,
  draftToAction,
  type PostCompletionAction,
  type PostCompletionDraft,
  type PostCompletionTrigger,
} from '@/lib/post-completion-action';

interface PostCompletionActionSelectProps {
  value: PostCompletionDraft;
  onChange: (next: PostCompletionDraft) => void;
  /** The task being edited — hidden from the target list (a task cannot re-run itself). */
  currentTaskId?: number;
  /** Stack the fields vertically (narrow sidebars). */
  compact?: boolean;
  disabled?: boolean;
}

interface SkillsResponse {
  skills?: { name: string }[];
}

/** How many recent tasks the target picker lists; the id input covers the rest. */
const TASK_PICKER_LIMIT = 200;

export function PostCompletionActionSelect({
  value,
  onChange,
  currentTaskId,
  compact = false,
  disabled = false,
}: PostCompletionActionSelectProps) {
  const tasks = useResource<Task[]>(
    (signal) => api.get<Task[]>(`/api/tasks?limit=${TASK_PICKER_LIMIT}`, { signal }),
    [],
    { enabled: value.kind === 'task', reloadOnRestore: false },
  );
  const skills = useResource<string[]>(
    async (signal) => {
      const body = await api.get<SkillsResponse>('/api/skills', { signal });
      return (body?.skills ?? []).map((skill) => skill.name);
    },
    [],
    { enabled: value.kind === 'skill', reloadOnRestore: false },
  );

  const update = (patch: Partial<PostCompletionDraft>) => onChange({ ...value, ...patch });
  const span = compact ? 12 : 4;

  const targetTasks = (tasks.data ?? []).filter((task) => task.id !== currentTaskId);
  const selectedListed = targetTasks.some((task) => String(task.id) === value.taskId);
  const skillNames = skills.data ?? [];

  return (
    <CRow className="g-2">
      <CCol md={span}>
        <CFormLabel className="small mb-1">Then run</CFormLabel>
        <CFormSelect
          size="sm"
          value={value.kind}
          disabled={disabled}
          onChange={(e) => update({ kind: e.target.value as PostCompletionDraft['kind'] })}
        >
          <option value="none">Nothing</option>
          <option value="task">Another task</option>
          <option value="skill">A skill (same repo)</option>
        </CFormSelect>
      </CCol>

      {value.kind === 'task' && (
        <CCol md={span}>
          <CFormLabel className="small mb-1">Task</CFormLabel>
          <CFormSelect
            size="sm"
            value={selectedListed ? value.taskId : ''}
            disabled={disabled}
            onChange={(e) => update({ taskId: e.target.value })}
          >
            <option value="">{tasks.loading ? 'Loading tasks…' : 'Pick a task (or type an id)'}</option>
            {targetTasks.map((task) => (
              <option key={task.id} value={String(task.id)}>
                #{task.id} {task.task_key} — {task.title.slice(0, 60)}
              </option>
            ))}
          </CFormSelect>
          <CFormInput
            size="sm"
            className="mt-1"
            inputMode="numeric"
            placeholder="Task id"
            aria-label="Target task id"
            value={value.taskId}
            disabled={disabled}
            onChange={(e) => update({ taskId: e.target.value.replace(/\D/g, '') })}
          />
        </CCol>
      )}

      {value.kind === 'skill' && (
        <CCol md={span}>
          <CFormLabel className="small mb-1">Skill</CFormLabel>
          <CFormInput
            size="sm"
            list="post-completion-skills"
            placeholder={skills.loading ? 'Loading skills…' : 'Skill name'}
            value={value.skillName}
            disabled={disabled}
            onChange={(e) => update({ skillName: e.target.value })}
          />
          <datalist id="post-completion-skills">
            {skillNames.map((name) => (
              <option key={name} value={name} />
            ))}
          </datalist>
        </CCol>
      )}

      {value.kind !== 'none' && (
        <CCol md={span}>
          <CFormLabel className="small mb-1">When</CFormLabel>
          <CFormSelect
            size="sm"
            value={value.trigger}
            disabled={disabled}
            onChange={(e) => update({ trigger: e.target.value as PostCompletionTrigger })}
          >
            {POST_COMPLETION_TRIGGERS.map((trigger) => (
              <option key={trigger} value={trigger}>
                {POST_COMPLETION_TRIGGER_LABELS[trigger]}
              </option>
            ))}
          </CFormSelect>
        </CCol>
      )}
    </CRow>
  );
}

interface PostCompletionActionCardProps {
  task: Pick<Task, 'id' | 'post_completion_action' | 'triggered_by_task_id'>;
}

/**
 * Task-detail sidebar card: edit and save one task's follow-up in place.
 * Self-contained (own draft, save, refresh) so the detail page mounts it with
 * a single element.
 */
export function PostCompletionActionCard({ task }: PostCompletionActionCardProps) {
  const router = useRouter();
  const [draft, setDraft] = useState<PostCompletionDraft>(() =>
    actionToDraft(task.post_completion_action ?? null),
  );
  const [validationError, setValidationError] = useState('');
  const [saved, setSaved] = useState(false);
  const save = useAsyncAction(async (value: PostCompletionAction | null) => {
    await api.patch(`/api/tasks/${task.id}`, { post_completion_action: value });
    router.refresh();
    return true;
  });

  const handleSave = async () => {
    setSaved(false);
    const parsed = draftToAction(draft);
    if (!parsed.ok) {
      setValidationError(parsed.error);
      return;
    }
    setValidationError('');
    // `run` never rejects: undefined means it failed and `save.error` says why.
    if (await save.run(parsed.value)) setSaved(true);
  };

  const shownError = validationError || (save.error ? errorMessage(save.error) : '');

  return (
    <CCard className="mb-4">
      <CCardHeader>
        <strong>On Completion</strong>
      </CCardHeader>
      <CCardBody>
        {shownError && <CAlert color="danger" className="py-2">{shownError}</CAlert>}
        {saved && !shownError && <CAlert color="success" className="py-2">Saved.</CAlert>}
        {task.triggered_by_task_id != null && (
          <p className="small mb-2">
            Last started by the completion of{' '}
            <Link href={`/tasks/${task.triggered_by_task_id}`}>task #{task.triggered_by_task_id}</Link>.
          </p>
        )}
        <PostCompletionActionSelect
          value={draft}
          onChange={(next) => {
            setDraft(next);
            setSaved(false);
          }}
          currentTaskId={task.id}
          compact
          disabled={save.busy}
        />
        <small className="text-body-secondary d-block mt-2">
          Cancelled tasks never trigger it; automatic chains stop after 3 hops.
        </small>
        <CButton color="primary" size="sm" className="mt-2" disabled={save.busy} onClick={handleSave}>
          {save.busy ? 'Saving…' : 'Save'}
        </CButton>
      </CCardBody>
    </CCard>
  );
}
