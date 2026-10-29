"""Discovery endpoint: what a client may put in a task.

Vendors and models come from the model registry in
``services.agent_backends``; the web app builds every model picker from this
response, so the registry stays the single place a model ID is written.
"""

from fastapi import APIRouter

from services import (
    agent_backends,
)

router = APIRouter(prefix="/internal")


# Task types the runner understands, and which of them need a repository.
_VALID_TASK_TYPES = {"coding", "test", "skill", "script", "research"}
_REPO_REQUIRED_TYPES = {"coding", "test", "script"}


@router.get("/agent-registry")
async def agent_registry():
    """Valid vendors / models / task types so a caller can build a task."""
    return {
        "vendors": agent_backends.registry_payload(),
        "default_vendor": agent_backends.DEFAULT_VENDOR,
        "task_types": sorted(_VALID_TASK_TYPES),
        "repo_required_types": sorted(_REPO_REQUIRED_TYPES),
        "git_flows": ["branch", "commit", "patch", "untracked"],
        "billing_modes": ["max", "api"],
        "modes": ["autonomous", "interactive"],
    }
