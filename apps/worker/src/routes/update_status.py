"""``GET /internal/update/status`` — the dashboard footer's "new version" source.

Why it exists: the web tier must not call GitHub itself on every page load;
the worker owns the cached, failure-silent check in
``services/release_check.py``. This router is the thin shell around it plus
the ``update_notify_enabled`` setting (default true): when an operator turns
it off, no network call is made at all — which matters for air-gapped hosts.
"""

from fastapi import APIRouter

from models.base import async_session
from services import release_check
from services.app_settings import get_bool_setting

router = APIRouter(prefix="/internal")

UPDATE_NOTIFY_SETTING = "update_notify_enabled"


@router.get("/update/status")
async def update_status(current: str | None = None) -> dict[str, str | None]:
    """Release status; ``current`` is the caller's build version (fallback only)."""
    try:
        async with async_session() as db:
            enabled = await get_bool_setting(db, UPDATE_NOTIFY_SETTING, True)
    except Exception:
        # No database must not turn into a noisy footer; behave as the default.
        enabled = True
    status = await release_check.get_update_status(enabled=enabled, current=current)
    return status.to_dict()
