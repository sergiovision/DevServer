export interface Repo {
  id: number;
  name: string;
  gitea_url: string;
  gitea_owner: string;
  gitea_repo: string;
  clone_url: string;
  default_branch: string;
  build_cmd: string | null;
  test_cmd: string | null;
  lint_cmd: string | null;
  pre_cmd: string | null;
  claude_model: string | null;
  claude_allowed_tools: string | null;
  max_retries: number;
  timeout_minutes: number;
  gitea_token: string;
  active: boolean;
  created_at: string;
  updated_at: string;
}

export type TaskPriority = 1 | 2 | 3 | 4;
export type TaskStatus =
  | 'pending'
  | 'queued'
  | 'running'
  | 'verifying'
  | 'test'
  | 'failed'
  | 'blocked'
  | 'cancelled'
  | 'retired';
export type TaskMode = 'autonomous' | 'interactive';
export type ClaudeMode = 'api' | 'max';
export type GitFlow = 'branch' | 'commit' | 'patch';
export type AgentVendor = 'anthropic' | 'google' | 'openai' | 'glm';

export interface Task {
  id: number;
  repo_id: number;
  task_key: string;
  title: string;
  description: string | null;
  acceptance: string | null;
  priority: TaskPriority;
  labels: string[];
  mode: TaskMode;
  claude_mode: ClaudeMode;
  agent_vendor: AgentVendor;
  claude_model: string | null;
  max_turns: number | null;
  status: TaskStatus;
  depends_on: number[];
  queue_job_id: string | null;
  skip_verify: boolean;
  git_flow: GitFlow;
  backup_vendor: AgentVendor | null;
  backup_model: string | null;
  created_by: string | null;
  created_at: string;
  updated_at: string;
  // Joined fields
  repo_name?: string;
  repo_clone_url?: string;
}

export interface TaskTemplate {
  id: number;
  name: string;
  description: string | null;
  acceptance: string | null;
  git_flow: GitFlow;
  claude_mode: ClaudeMode;
  agent_vendor: AgentVendor;
  claude_model: string | null;
  backup_vendor: AgentVendor | null;
  backup_model: string | null;
  max_turns: number | null;
  skip_verify: boolean;
  created_at: string;
  updated_at: string;
}

export type RunStatus = 'running' | 'passed' | 'failed' | 'cancelled';

export interface TaskRun {
  id: number;
  task_id: number;
  attempt: number;
  session_id: string | null;
  branch: string | null;
  pr_url: string | null;
  status: RunStatus;
  cost_usd: number | null;
  duration_ms: number | null;
  turns: number | null;
  error_log: string | null;
  claude_output: string | null;
  created_at: string;
  updated_at: string;
}

export interface TaskEvent {
  id: number;
  task_id: number;
  run_id: number | null;
  event_type: string;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface GhostJobInfo {
  detected: boolean;
  queue_active: boolean;
  worker_knows: boolean;
  lock_held: boolean;
}

export interface Settings {
  key: string;
  value: unknown;
}

export interface DailyStats {
  date: string;
  completed: number;
  failed: number;
  cost_usd: number;
  total_duration_ms: number;
  total_turns: number;
}

export interface QueueStatsResponse {
  waiting: number;
  active: number;
  completed: number;
  failed: number;
  delayed: number;
  paused: number;
}

export const PRIORITY_LABELS: Record<TaskPriority, string> = {
  1: 'Critical',
  2: 'High',
  3: 'Medium',
  4: 'Low',
};

export const PRIORITY_COLORS: Record<TaskPriority, string> = {
  1: 'danger',
  2: 'warning',
  3: 'info',
  4: 'secondary',
};

export const STATUS_COLORS: Record<TaskStatus, string> = {
  running: 'success',
  pending: 'secondary',
  queued: 'info',
  verifying: 'warning',
  test: 'info',
  failed: 'danger',
  blocked: 'dark',
  cancelled: 'secondary',
  retired: 'dark',
};
