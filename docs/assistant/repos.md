# Repositories and git flows

Add repositories on **Repos** (`/repos`). Each one records where the code lives,
how to build and test it, and which agent defaults to use.

## Three providers

| Provider | What it means |
|---|---|
| **Gitea** | Self-hosted Gitea. Clone URL + token; DevServer opens PRs through the Gitea API. |
| **GitHub** | Auto-detected from a `github.com` clone URL. Uses a `Bearer` token against the GitHub API. |
| **Local Git** | A folder that already exists on the worker machine. No clone, no remote, never pushed. |

A per-repo token always wins over the global one. GitHub repos never fall back
to the Gitea token — that pairing produces a confusing *"Invalid username or
token"* error, so DevServer refuses it outright.

**Local Git** is the odd one out and deliberately conservative: DevServer runs
git commands directly in your folder, never pushes, and never resets or cleans
it. Only two git flows are allowed (*Untracked* and *Patch*), and the folder
path goes in the **Local Root Folder** field.

## Repo settings that matter

- **Build / Test / Lint / Pre commands** — what the verifier runs. Leave a field
  empty to skip that check.
- **Default branch**, **max retries**, **timeout**.
- **Allowed tools** — which agent tools may be used in this repo.
- **CLAUDE.md path** — the instructions file the agent reads on arrival.

## Git flows

Chosen per task. From least to most invasive:

| Flow | What happens |
|---|---|
| **Untracked** (default) | The agent edits files in an isolated worktree and reports. No branch, no commit, no push, no PR. |
| **Direct Commit** | Squash-merges onto the default branch. No PR. |
| **Patch** | Commits on a local `agent/{task_key}` branch and produces a `combined.mbox` you can `git am` elsewhere. Nothing is pushed. |
| **Branch + PR** | Creates `agent/{task_key}`, pushes it, and opens a pull request. |

For a local repo, *Untracked* means the agent edits your actual folder without
committing, and *Patch* checks out an `agent/…` branch, commits there, generates
the patch, and puts your original branch back.

## Isolation

Remote repos are cloned once into a bare mirror and each task gets its own git
worktree. That is why two tasks on the same repo do not collide, and why an
agent can never damage your checkout. After a task finishes, its worktree is
reset and cleaned — except for local repos, where the folder is yours and
DevServer leaves it alone.

## Other repo tools

- **Refresh Git** — re-fetches the mirror (for a local repo, revalidates the folder).
- **Architecture diagram** — an instant Mermaid module map, computed locally with no LLM call.
- **Reindex** — rebuilds the semantic code/doc index for the repo.
