"""Enhanced health check endpoints for DevServer worker."""

import asyncio
import time
from typing import Dict, List, Any
from fastapi import APIRouter
from fastapi.responses import JSONResponse
import asyncpg
from config import settings

router = APIRouter()


async def check_database() -> Dict[str, Any]:
    """Check PostgreSQL database connectivity."""
    start_time = time.time()
    try:
        conn = await asyncpg.connect(settings.database_url)
        await conn.fetchval("SELECT 1")
        await conn.close()

        response_time = (time.time() - start_time) * 1000  # Convert to ms

        return {
            "name": "database",
            "status": "degraded" if response_time > 5000 else "healthy",
            "response_time": round(response_time, 2),
            "details": {
                "database_url": settings.database_url.split("@")[1] if "@" in settings.database_url else "configured"
            }
        }
    except Exception as e:
        return {
            "name": "database",
            "status": "unhealthy",
            "response_time": round((time.time() - start_time) * 1000, 2),
            "error": str(e)
        }


async def check_queue_system() -> Dict[str, Any]:
    """Check PgQueuer system status."""
    start_time = time.time()
    try:
        conn = await asyncpg.connect(settings.database_url)

        # Get queue stats. NOTE: pgqueuer_status is an enum
        # ('queued','picked','successful','exception','canceled','deleted') —
        # there is no 'failed' value, so a failed job is 'exception'. Using an
        # invalid enum literal here would raise and fail the whole probe.
        stats = await conn.fetchrow("""
            SELECT
                COUNT(*) as total_jobs,
                COUNT(*) FILTER (WHERE status = 'queued') as queued_jobs,
                COUNT(*) FILTER (WHERE status = 'picked') as running_jobs,
                COUNT(*) FILTER (WHERE status = 'exception') as failed_jobs
            FROM pgqueuer
            WHERE entrypoint = 'devserver-tasks'
        """)

        await conn.close()

        response_time = (time.time() - start_time) * 1000

        total_jobs = stats['total_jobs'] if stats else 0
        failed_jobs = stats['failed_jobs'] if stats else 0
        failure_rate = failed_jobs / max(total_jobs, 1)

        status = "degraded" if failure_rate > 0.5 else "healthy"

        return {
            "name": "queue_system",
            "status": status,
            "response_time": round(response_time, 2),
            "details": {
                "total_jobs": total_jobs,
                "queued_jobs": stats['queued_jobs'] if stats else 0,
                "running_jobs": stats['running_jobs'] if stats else 0,
                "failed_jobs": failed_jobs,
                "failure_rate": round(failure_rate, 2)
            }
        }
    except Exception as e:
        return {
            "name": "queue_system",
            "status": "unhealthy",
            "response_time": round((time.time() - start_time) * 1000, 2),
            "error": str(e)
        }


async def check_file_system() -> Dict[str, Any]:
    """Check file system access for critical directories."""
    start_time = time.time()
    try:
        import os
        import tempfile

        # Test write access to worktree and log directories
        directories_to_check = [
            settings.worktree_dir,
            settings.log_dir
        ]

        details = {}
        for directory in directories_to_check:
            if directory:
                os.makedirs(directory, exist_ok=True)
                test_file = os.path.join(directory, f"health_check_{time.time()}.tmp")
                with open(test_file, 'w') as f:
                    f.write("health check")
                with open(test_file, 'r') as f:
                    content = f.read()
                os.unlink(test_file)
                details[directory] = "writable"

        response_time = (time.time() - start_time) * 1000

        return {
            "name": "file_system",
            "status": "degraded" if response_time > 1000 else "healthy",
            "response_time": round(response_time, 2),
            "details": details
        }
    except Exception as e:
        return {
            "name": "file_system",
            "status": "unhealthy",
            "response_time": round((time.time() - start_time) * 1000, 2),
            "error": str(e)
        }


def calculate_overall_status(checks: List[Dict[str, Any]]) -> str:
    """Calculate overall health status from individual checks."""
    if any(check["status"] == "unhealthy" for check in checks):
        return "unhealthy"
    if any(check["status"] == "degraded" for check in checks):
        return "degraded"
    return "healthy"


def create_summary(checks: List[Dict[str, Any]]) -> Dict[str, int]:
    """Create health check summary."""
    return {
        "total": len(checks),
        "healthy": sum(1 for check in checks if check["status"] == "healthy"),
        "unhealthy": sum(1 for check in checks if check["status"] == "unhealthy"),
        "degraded": sum(1 for check in checks if check["status"] == "degraded")
    }


# NOTE: the simple GET /health endpoint is served by routes/health.py (kept
# for backward compatibility). This router only adds the detailed/readiness
# probes below to avoid registering a duplicate /health handler.


@router.get("/health/detailed")
async def detailed_health():
    """Detailed health check endpoint with comprehensive dependency checking."""
    timestamp = time.time()
    iso_timestamp = time.strftime("%Y-%m-%dT%H:%M:%S.%fZ", time.gmtime(timestamp))

    try:
        # Run all health checks in parallel
        checks = await asyncio.gather(
            check_database(),
            check_queue_system(),
            check_file_system(),
            return_exceptions=True
        )

        # Handle any exceptions from the checks
        valid_checks = []
        for i, check in enumerate(checks):
            if isinstance(check, Exception):
                valid_checks.append({
                    "name": f"check_{i}",
                    "status": "unhealthy",
                    "error": str(check)
                })
            else:
                valid_checks.append(check)

        overall_status = calculate_overall_status(valid_checks)
        summary = create_summary(valid_checks)

        response = {
            "status": overall_status,
            "service": "devserver-worker",
            "timestamp": iso_timestamp,
            "checks": valid_checks,
            "summary": summary
        }

        # Return appropriate HTTP status
        status_code = 200 if overall_status == "healthy" else 206 if overall_status == "degraded" else 503

        return JSONResponse(
            content=response,
            status_code=status_code,
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "X-Health-Status": overall_status
            }
        )

    except Exception as e:
        # Fallback response
        fallback_response = {
            "status": "unhealthy",
            "service": "devserver-worker",
            "timestamp": iso_timestamp,
            "checks": [{
                "name": "health_system",
                "status": "unhealthy",
                "error": str(e)
            }],
            "summary": {
                "total": 1,
                "healthy": 0,
                "unhealthy": 1,
                "degraded": 0
            }
        }

        return JSONResponse(
            content=fallback_response,
            status_code=503,
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "X-Health-Status": "unhealthy"
            }
        )


@router.get("/ready")
async def readiness():
    """Readiness endpoint - checks if worker is ready to process tasks."""
    timestamp = time.time()
    iso_timestamp = time.strftime("%Y-%m-%dT%H:%M:%S.%fZ", time.gmtime(timestamp))

    try:
        # Check only critical dependencies for readiness
        checks = await asyncio.gather(
            check_database(),
            check_queue_system(),
            return_exceptions=True
        )

        valid_checks = []
        for i, check in enumerate(checks):
            if isinstance(check, Exception):
                valid_checks.append({
                    "name": f"critical_check_{i}",
                    "status": "unhealthy",
                    "error": str(check)
                })
            else:
                valid_checks.append(check)

        critical_issues = [check for check in valid_checks if check["status"] == "unhealthy"]
        is_ready = len(critical_issues) == 0

        summary = create_summary(valid_checks)

        response = {
            "status": "healthy" if is_ready else "unhealthy",
            "ready": is_ready,
            "service": "devserver-worker",
            "timestamp": iso_timestamp,
            "checks": valid_checks,
            "summary": summary,
            "critical_issues": [
                {"service": check["name"], "error": check.get("error")}
                for check in critical_issues
            ]
        }

        status_code = 200 if is_ready else 503

        return JSONResponse(
            content=response,
            status_code=status_code,
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "X-Ready-Status": "ready" if is_ready else "not-ready"
            }
        )

    except Exception as e:
        fallback_response = {
            "status": "unhealthy",
            "ready": False,
            "service": "devserver-worker",
            "timestamp": iso_timestamp,
            "checks": [{
                "name": "readiness_system",
                "status": "unhealthy",
                "error": str(e)
            }],
            "summary": {"total": 1, "healthy": 0, "unhealthy": 1, "degraded": 0},
            "critical_issues": [{"service": "readiness_system", "error": str(e)}]
        }

        return JSONResponse(
            content=fallback_response,
            status_code=503,
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "X-Ready-Status": "not-ready"
            }
        )