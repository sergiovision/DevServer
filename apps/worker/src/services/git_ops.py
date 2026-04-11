"""Git worktree management and Gitea PR creation.

One persistent worktree per repo. Tasks for the same repo run consecutively
(enforced by the repo lock in agent_runner). Each task resets the worktree
to the default branch and checks out a fresh task branch.
"""

import asyncio
import logging
import os

import httpx

from config import settings

logger = logging.getLogger(__name__)


async def _run(cmd: list[str], cwd: str | None = None) -> tuple[int, str, str]:
    """Run a subprocess, return (returncode, stdout, stderr)."""
    env = {**os.environ}
    if settings.git_ssl_no_verify:
        env["GIT_SSL_NO_VERIFY"] = "true"
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode(), stderr.decode()


def _auth_url(clone_url: str, token: str) -> str:
    """Inject token into HTTPS clone URL."""
    return clone_url.replace("https://", f"https://token:{token}@", 1)


def get_worktree_path(repo_name: str) -> str:
    """Return the persistent worktree path for a repo."""
    return os.path.join(settings.worktree_dir, repo_name)


async def setup_worktree(
    repo_name: str,
    clone_url: str,
    default_branch: str,
    task_key: str,
    gitea_token: str | None = None,
) -> tuple[str, str]:
    """Prepare the per-repo worktree for a new task.

    - Creates bare repo + worktree on first use.
    - On subsequent tasks: fetches latest, resets to default branch, creates task branch.

    Returns (worktree_path, branch_name).
    """
    token = gitea_token or settings.gitea_token or ""
    auth_url = _auth_url(clone_url, token)

    bare_repo = os.path.join(settings.bare_repo_dir, repo_name)
    # Sanitize: spaces and other invalid chars → hyphens, lowercase
    safe_key = task_key.replace(" ", "-").replace("/", "-").strip("-")
    branch_name = f"agent/{safe_key}"
    worktree_path = get_worktree_path(repo_name)

    # --- Ensure bare repo exists ---
    if not os.path.isdir(bare_repo):
        os.makedirs(os.path.dirname(bare_repo), exist_ok=True)
        logger.info("Cloning bare repo %s", repo_name)
        rc, out, err = await _run(["git", "clone", "--bare", auth_url, bare_repo])
        if rc != 0:
            raise RuntimeError(f"git clone --bare failed: {err}")

    # Update remote URL (token may have changed)
    await _run(["git", "-C", bare_repo, "remote", "set-url", "origin", auth_url])

    # Fetch latest from remote
    rc, out, err = await _run([
        "git", "-C", bare_repo, "fetch", "origin",
        "+refs/heads/*:refs/heads/*", "--prune",
    ])
    if rc != 0:
        logger.warning("git fetch warning: %s", err)

    # --- Ensure worktree exists ---
    if not os.path.isdir(worktree_path):
        logger.info("Creating worktree for %s at %s", repo_name, worktree_path)
        rc, out, err = await _run([
            "git", "-C", bare_repo, "worktree", "add",
            worktree_path, default_branch,
        ])
        if rc != 0:
            raise RuntimeError(f"git worktree add failed: {err}")
    else:
        logger.info("Reusing existing worktree for %s", repo_name)

    # Configure git identity
    await _run(["git", "-C", worktree_path, "config", "user.email", settings.git_user_email])
    await _run(["git", "-C", worktree_path, "config", "user.name", settings.git_user_name])

    # --- Reset worktree to a clean default branch state ---
    # Hard reset first to discard any modified tracked files (e.g. packages.lock.json),
    # then checkout. This avoids "please commit or stash" errors.
    await _run(["git", "-C", worktree_path, "reset", "--hard"])
    rc, out, err = await _run(
        ["git", "-C", worktree_path, "checkout", default_branch],
    )
    if rc != 0:
        logger.warning("Checkout %s failed (%s), forcing", default_branch, err.strip())
        await _run(["git", "-C", worktree_path, "checkout", "--force", default_branch])

    # Hard reset to remote state
    await _run([
        "git", "-C", worktree_path, "reset", "--hard", f"origin/{default_branch}",
    ])

    # Mark generated lock files as skip-worktree so dotnet restore doesn't dirty the index
    rc, out, _ = await _run(
        ["git", "-C", worktree_path, "ls-files", "--", "**/packages.lock.json", "packages.lock.json"],
    )
    lock_files = [f for f in out.splitlines() if f.strip()]
    if lock_files:
        await _run(
            ["git", "-C", worktree_path, "update-index", "--skip-worktree"] + lock_files
        )

    # Remove untracked files
    await _run(["git", "-C", worktree_path, "clean", "-fdx", "--exclude=.env"])

    # Check if task branch already exists with committed work
    rc_check, _, _ = await _run(
        ["git", "-C", worktree_path, "rev-parse", "--verify", branch_name],
    )
    if rc_check == 0:
        # Branch exists — check if it has commits ahead of default_branch
        rc_ahead, ahead_out, _ = await _run([
            "git", "-C", worktree_path, "rev-list", "--count",
            f"{default_branch}..{branch_name}",
        ])
        commits_ahead = int(ahead_out.strip() or "0") if rc_ahead == 0 else 0
        if commits_ahead > 0:
            # Previous run made progress — resume from existing branch
            logger.info(
                "Resuming task branch %s (%d commits ahead of %s)",
                branch_name, commits_ahead, default_branch,
            )
            rc, out, err = await _run(
                ["git", "-C", worktree_path, "checkout", branch_name],
            )
            if rc != 0:
                raise RuntimeError(f"git checkout {branch_name} failed: {err}")
            logger.info("Worktree ready (resumed): %s on branch %s", worktree_path, branch_name)
            return worktree_path, branch_name
        else:
            # Branch exists but empty — delete and start fresh
            await _run(["git", "-C", worktree_path, "branch", "-D", branch_name])
    # else: branch doesn't exist, nothing to delete

    # Create fresh task branch
    rc, out, err = await _run([
        "git", "-C", worktree_path, "checkout", "-b", branch_name,
    ])
    if rc != 0:
        raise RuntimeError(f"git checkout -b {branch_name} failed: {err}")

    logger.info("Worktree ready: %s on branch %s", worktree_path, branch_name)
    return worktree_path, branch_name


async def reset_worktree(repo_name: str, default_branch: str) -> None:
    """Reset the worktree back to default branch after task completion.

    Called in the finally block so the next task always starts clean.
    """
    worktree_path = get_worktree_path(repo_name)
    if not os.path.isdir(worktree_path):
        return

    logger.info("Resetting worktree %s to %s", repo_name, default_branch)
    await _run(["git", "-C", worktree_path, "reset", "--hard"])
    await _run(["git", "-C", worktree_path, "checkout", "--force", default_branch])
    await _run(["git", "-C", worktree_path, "reset", "--hard", f"origin/{default_branch}"])
    await _run(["git", "-C", worktree_path, "clean", "-fdx", "--exclude=.env"])


async def ensure_committed(worktree_path: str, task_key: str, title: str) -> bool:
    """Commit any uncommitted changes. Returns True if a commit was made."""
    rc, stdout, _ = await _run(["git", "status", "--porcelain"], cwd=worktree_path)
    if not stdout.strip():
        return False

    logger.info("Uncommitted changes detected — committing")
    await _run(["git", "add", "-A"], cwd=worktree_path)
    msg = f"[{task_key}] {title}\n\nGenerated by DevServer autonomous agent"
    await _run(["git", "commit", "-m", msg], cwd=worktree_path)
    return True


async def commit_to_default_branch(
    worktree_path: str,
    branch_name: str,
    default_branch: str,
    task_key: str,
    title: str,
) -> bool:
    """Squash-merge the task branch directly onto default_branch and push.

    Used by git_flow='commit' tasks that want a single clean commit on the
    main branch without a pull request.  Returns True on success.
    """
    # Switch to default branch and pull latest
    rc, _, err = await _run(["git", "checkout", default_branch], cwd=worktree_path)
    if rc != 0:
        logger.error("checkout %s failed: %s", default_branch, err)
        return False

    rc, _, err = await _run(
        ["git", "pull", "--ff-only", "origin", default_branch], cwd=worktree_path
    )
    if rc != 0:
        logger.warning("pull %s non-fast-forward, resetting to origin: %s", default_branch, err)
        await _run(["git", "reset", "--hard", f"origin/{default_branch}"], cwd=worktree_path)

    # Squash all task-branch commits into a single staged change
    rc, _, err = await _run(
        ["git", "merge", "--squash", branch_name], cwd=worktree_path
    )
    if rc != 0:
        logger.error("squash merge %s failed: %s", branch_name, err)
        return False

    msg = f"[{task_key}] {title}\n\nGenerated by DevServer autonomous agent"
    rc, _, err = await _run(["git", "commit", "-m", msg], cwd=worktree_path)
    if rc != 0:
        logger.error("commit after squash merge failed: %s", err)
        return False

    rc, _, err = await _run(
        ["git", "push", "origin", default_branch], cwd=worktree_path
    )
    if rc != 0:
        logger.error("push %s failed: %s", default_branch, err)
        return False

    logger.info("git_flow=commit: squash-merged %s → %s", branch_name, default_branch)
    return True


async def create_gitea_pr(
    worktree_path: str,
    branch_name: str,
    default_branch: str,
    title: str,
    body: str,
    gitea_url: str | None = None,
    gitea_owner: str | None = None,
    gitea_repo: str | None = None,
    gitea_token: str | None = None,
) -> str | None:
    """Push branch and create a Gitea pull request. Returns PR URL or None."""
    # Push branch
    rc, out, err = await _run(
        ["git", "push", "origin", branch_name, "--force-with-lease"],
        cwd=worktree_path,
    )
    if rc != 0:
        logger.error("git push failed: %s", err)
        return None

    # Create PR via Gitea API
    token = gitea_token or settings.gitea_token or ""
    base_url = gitea_url or settings.gitea_url
    owner = gitea_owner or settings.gitea_owner
    repo = gitea_repo or ""

    api_url = f"{base_url}/api/v1/repos/{owner}/{repo}/pulls"
    try:
        async with httpx.AsyncClient(timeout=30, verify=not settings.git_ssl_no_verify) as client:
            resp = await client.post(
                api_url,
                headers={
                    "Authorization": f"token {token}",
                    "Content-Type": "application/json",
                },
                json={
                    "title": title,
                    "body": body,
                    "head": branch_name,
                    "base": default_branch,
                },
            )
            data = resp.json()
            pr_url = data.get("html_url")
            if not pr_url:
                logger.error("Gitea PR creation failed: %s", data)
                return None
            logger.info("PR created: %s", pr_url)
            return pr_url
    except Exception:
        logger.exception("Gitea PR creation error")
        return None
