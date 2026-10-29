"""Everything one task run knows, in one place.

``TaskSpec`` and ``RepoSpec`` are frozen snapshots taken when the run starts,
so no phase holds an ORM object (and with it a database session) while it
waits on a subprocess. ``TaskContext`` adds the state that evolves over the
run: the workspace, gathered evidence, spend so far, the session to resume.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal

from models.base import async_session
from models.repo import Repo
from models.task import Task
from services import agent_backends, git_ops
from services.budget import BudgetLimits

from .task_log import TaskLog

logger = logging.getLogger(__name__)

#: Turn cap when neither the job nor the task sets one.
DEFAULT_MAX_TURNS = 50
#: Job-payload sentinel for "no turn cap" (the long-running preset).
UNLIMITED_TURNS = -1


@dataclass(frozen=True)
class TaskSpec:
    id: int
    key: str
    title: str
    description: str
    acceptance: str
    task_type: str
    mode: str
    skip_verify: bool
    git_flow: str
    is_continuation: bool
    vendor: str
    model: str
    backup_vendor: str | None
    backup_model: str | None
    max_turns: int | None
    max_cost_usd: Decimal | None
    max_wall_seconds: int | None
    compacted_context: str

    @property
    def runs_own_checks(self) -> bool:
        """Test and script tasks run their checks through the agent itself."""
        return self.task_type in ("test", "script")

    @property
    def verifier_off(self) -> bool:
        return self.skip_verify or self.runs_own_checks


@dataclass(frozen=True)
class RepoSpec:
    id: int
    name: str
    provider: str
    default_branch: str
    clone_url: str
    gitea_url: str
    gitea_owner: str
    gitea_repo: str
    gitea_token: str
    pre_cmd: str
    build_cmd: str
    test_cmd: str
    lint_cmd: str
    allowed_tools: str
    max_retries: int
    timeout_minutes: int

    @property
    def is_local(self) -> bool:
        """A folder on the worker host: no clone, no push, no PR."""
        return git_ops.is_local_provider(self.provider)


@dataclass
class Spend:
    """Agent-active cost and wall time, the inputs of the budget check."""

    cost_usd: Decimal = Decimal("0")
    wall_ms: int = 0


@dataclass
class Evidence:
    """Context blocks gathered once, before the first attempt."""

    repo_map_text: str = ""
    reality_signal: dict = field(default_factory=dict)
    reality_text: str = ""
    memory_text: str = ""


@dataclass
class TaskContext:
    task: TaskSpec
    repo: RepoSpec
    billing_mode: str
    max_turns: int | None
    log: TaskLog
    model: str
    branch_name: str
    git_flow: str
    worktree_path: str = ""
    #: The branch a local repo had checked out before the run (restored after).
    local_original_branch: str | None = None
    evidence: Evidence = field(default_factory=Evidence)
    approved_plan_text: str = ""
    preflight_allowlist: list[str] | None = None
    gate_prompt: str = ""
    skill_prompt: str = ""
    error_context: str = ""
    session_id: str | None = None
    compacted_context: str = ""
    auto_compacted: bool = False
    spend: Spend = field(default_factory=Spend)
    error_class_counts: Counter[str] = field(default_factory=Counter)
    budget_warned: bool = False
    #: "vendor/model" once the run has failed over to the backup model.
    failover_label: str | None = None

    @property
    def budget_limits(self) -> BudgetLimits:
        return BudgetLimits(
            max_cost_usd=self.task.max_cost_usd,
            max_wall_seconds=self.task.max_wall_seconds,
            billing_mode=self.billing_mode,
        )


def _task_spec(task: Task) -> TaskSpec:
    return TaskSpec(
        id=task.id,
        key=task.task_key,
        title=task.title,
        description=task.description or "",
        acceptance=task.acceptance or "",
        task_type=(task.task_type or "coding").lower(),
        mode=task.mode or "autonomous",
        skip_verify=bool(task.skip_verify),
        git_flow=task.git_flow or "branch",
        is_continuation=bool(task.is_continuation),
        vendor=task.agent_vendor or agent_backends.DEFAULT_VENDOR,
        model=task.claude_model or "",
        backup_vendor=task.backup_vendor,
        backup_model=task.backup_model,
        max_turns=task.max_turns,
        max_cost_usd=task.max_cost_usd,
        max_wall_seconds=task.max_wall_seconds,
        compacted_context=task.compacted_context or "",
    )


def _repo_spec(repo: Repo) -> RepoSpec:
    return RepoSpec(
        id=repo.id,
        name=repo.name,
        provider=repo.provider or "",
        default_branch=repo.default_branch,
        clone_url=repo.clone_url or "",
        gitea_url=repo.gitea_url or "",
        gitea_owner=repo.gitea_owner or "",
        gitea_repo=repo.gitea_repo or "",
        gitea_token=repo.gitea_token or "",
        pre_cmd=repo.pre_cmd or "",
        build_cmd=repo.build_cmd or "",
        test_cmd=repo.test_cmd or "",
        lint_cmd=repo.lint_cmd or "",
        allowed_tools=repo.claude_allowed_tools,
        max_retries=repo.max_retries,
        timeout_minutes=repo.timeout_minutes,
    )


async def load_specs(task_id: int) -> tuple[TaskSpec, RepoSpec | None, str] | None:
    """Snapshot the task and its repo. Returns ``(task, repo, repo_default_model)``."""
    async with async_session() as db:
        task = await db.get(Task, task_id)
        if task is None:
            logger.error("Task %d not found", task_id)
            return None
        repo = await db.get(Repo, task.repo_id) if task.repo_id is not None else None
        repo_model = repo.claude_model if repo is not None else ""
        return _task_spec(task), (_repo_spec(repo) if repo else None), repo_model or ""


def resolve_max_turns(job_max_turns: int | None, task_max_turns: int | None) -> int | None:
    """Job payload → task column → default; ``-1`` means unlimited (``None``)."""
    if job_max_turns is not None:
        turns = job_max_turns
    elif task_max_turns is not None:
        turns = task_max_turns
    else:
        turns = DEFAULT_MAX_TURNS
    return None if turns == UNLIMITED_TURNS else turns


def resolve_git_flow(task: TaskSpec, repo: RepoSpec) -> str:
    """Local repos support only 'patch' and 'untracked'; coerce the rest."""
    if repo.is_local and task.git_flow not in ("patch", "untracked"):
        logger.warning(
            "Task %s: git_flow=%r is not supported for local repos — using 'patch'",
            task.key,
            task.git_flow,
        )
        return "patch"
    return task.git_flow
