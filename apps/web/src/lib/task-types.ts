import type { TaskType, GitFlow } from './types';

/**
 * Task-type registry — the single source of truth (web side) for the label,
 * description, and per-type field visibility used by the create form, the
 * template form, and the task detail page.
 *
 * The Python worker is the behavioural authority (services/agent_runner.py +
 * services/skill_runner.py decide how each type actually runs). This module
 * only drives the UI. Keep the `id` list in sync with the worker's task_type
 * values and the DB column default.
 */
export interface TaskTypeEntry {
  id: TaskType;
  label: string;
  /** One-line explanation shown under the type picker. */
  description: string;
  /** A repository must be selected for this type. */
  requiresRepo: boolean;
  /** Show the Acceptance Criteria field. */
  showAcceptance: boolean;
  /** Label for the acceptance field (some types repurpose it). */
  acceptanceLabel: string;
  acceptancePlaceholder: string;
  /** Show the Skip-verification checkbox. */
  showSkipVerify: boolean;
  /** Show the Git-flow control (branch/commit/patch/…). */
  showGitFlow: boolean;
  /**
   * Default git flow for new tasks of this type. All types default to
   * `untracked` (no branch, no commit, no push, no PR) — the safest, least
   * surprising option. The other flows (`commit`, `patch`, `branch`) are
   * offered for every type/repo and the operator opts in explicitly when
   * they want a commit, patch, or PR.
   */
  defaultGitFlow: GitFlow;
  /** Placeholder/help for the Description field (varies by type). */
  descriptionPlaceholder: string;
}

export const TASK_TYPES: TaskTypeEntry[] = [
  {
    id: 'coding',
    label: 'Coding',
    description: 'Implement a code change, verify it, and open a PR.',
    requiresRepo: true,
    showAcceptance: true,
    acceptanceLabel: 'Acceptance Criteria',
    acceptancePlaceholder:
      'What conditions must be met for this task to be considered done?',
    showSkipVerify: true,
    showGitFlow: true,
    defaultGitFlow: 'untracked',
    descriptionPlaceholder: 'Detailed task description…',
  },
  {
    id: 'test',
    label: 'Test',
    description:
      'Run the tests specified in the description. Acceptance = which tests must pass.',
    requiresRepo: true,
    showAcceptance: true,
    acceptanceLabel: 'Which tests must pass',
    acceptancePlaceholder:
      'List the tests that must pass, e.g. tests/auth/test_login.py::test_ok',
    showSkipVerify: false,
    showGitFlow: true,
    defaultGitFlow: 'untracked',
    descriptionPlaceholder:
      'Which tests to run and how (test runner, file paths, markers, params)…',
  },
  {
    id: 'skill',
    label: 'Skill',
    description:
      'Run a skill with the selected AI provider, described in the description (params + conditions).',
    requiresRepo: false,
    showAcceptance: false,
    acceptanceLabel: 'Acceptance Criteria',
    acceptancePlaceholder: '',
    showSkipVerify: false,
    showGitFlow: false,
    defaultGitFlow: 'untracked',
    descriptionPlaceholder:
      'Which skill to run, with what parameters and under what conditions…',
  },
  {
    id: 'script',
    label: 'Script',
    description:
      'Run a script (bash/python/ts/…) at a given location with params in a working folder.',
    requiresRepo: true,
    showAcceptance: true,
    acceptanceLabel: 'Expected result',
    acceptancePlaceholder: 'Expected output / exit code for a successful run…',
    showSkipVerify: false,
    showGitFlow: true,
    defaultGitFlow: 'untracked',
    descriptionPlaceholder:
      'Script location, language/interpreter, parameters, and working folder…',
  },
  {
    id: 'research',
    label: 'Research',
    description:
      'Ask the AI model a prompt (the description IS the prompt). The answer is printed to the Task Log.',
    requiresRepo: false,
    showAcceptance: false,
    acceptanceLabel: 'Acceptance Criteria',
    acceptancePlaceholder: '',
    showSkipVerify: false,
    showGitFlow: false,
    defaultGitFlow: 'untracked',
    descriptionPlaceholder: 'The full research prompt to send to the model…',
  },
];

export const DEFAULT_TASK_TYPE: TaskType = 'coding';

export function taskTypeEntry(id: string | null | undefined): TaskTypeEntry {
  return TASK_TYPES.find((t) => t.id === id) ?? TASK_TYPES[0];
}

export function taskTypeLabel(id: string | null | undefined): string {
  return taskTypeEntry(id).label;
}

/** CoreUI badge colour per task type (dashboard badges). */
export const TASK_TYPE_BADGE: Record<TaskType, string> = {
  coding: 'primary',
  test: 'info',
  skill: 'warning',
  script: 'success',
  research: 'secondary',
};
