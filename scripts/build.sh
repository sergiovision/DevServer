#!/bin/bash
# DevServer — build for dev, prod, or docker.
#
# Usage:
#   ./scripts/build.sh              # dev (default)
#   ./scripts/build.sh --dev         # worker venv + deps, web npm deps (no Next build)
#   ./scripts/build.sh --prod        # worker venv + deps, web npm deps + Next.js build
#   ./scripts/build.sh --docker      # docker compose build
#
# What "build" means per mode:
#   --dev    Prepare the dev environment so `start.sh --dev` runs instantly.
#            Creates the worker venv (uv or python3 -m venv), installs the worker
#            package editable, runs `npm ci` if node_modules is missing.
#            Skips `next build` — dev mode uses tsx hot reload.
#
#   --prod   Everything --dev does, PLUS `npm run build` (next build + tsc of
#            server.ts → server.js), producing the artifacts `start.sh --prod` needs.
#
#   --docker `docker compose build` on the stack in docker/docker-compose.yml.
#
# Cross-platform: works on Linux and macOS (uv preferred, python3 -m venv fallback).
set -euo pipefail

source "$(dirname "$0")/_lib.sh"
MODE="$(parse_mode "$@")"

# ── Worker venv + package install ─────────────────────────────────────────────
# Mirrors the exact logic in start.sh so behavior stays consistent.
build_worker() {
  echo "Building worker..."

  # Create venv if missing: prefer `uv venv` (project standard), else python3 -m venv.
  if [[ ! -f "${WORKER_DIR}/.venv/bin/activate" ]]; then
    echo "  Creating Python venv..."
    if command -v uv >/dev/null 2>&1; then
      (cd "${WORKER_DIR}" && uv venv)
    else
      python3 -m venv "${WORKER_DIR}/.venv"
    fi
  fi

  # Install/refresh the worker package. `uv venv` does not include pip, so prefer uv.
  echo "  Installing worker package..."
  if [[ -x "${WORKER_DIR}/.venv/bin/pip" ]]; then
    "${WORKER_DIR}/.venv/bin/pip" install -q -e "${WORKER_DIR}"
  elif command -v uv >/dev/null 2>&1; then
    (cd "${WORKER_DIR}" && VIRTUAL_ENV="${WORKER_DIR}/.venv" uv pip install -q -e .)
  else
    "${WORKER_DIR}/.venv/bin/python" -m ensurepip --upgrade >/dev/null 2>&1 || {
      red "Neither uv nor pip is available in the worker venv."
      red "Install uv (https://docs.astral.sh/uv/) or recreate the venv with 'python3 -m venv'."
      exit 1
    }
    "${WORKER_DIR}/.venv/bin/python" -m pip install -q -e "${WORKER_DIR}"
  fi

  green "  Worker ready (venv + package installed)"
}

# ── Web dependencies (npm ci) ─────────────────────────────────────────────────
build_web_deps() {
  if [[ ! -d "${WEB_DIR}/node_modules" ]]; then
    echo "  Installing npm dependencies..."
    npm --prefix "${WEB_DIR}" ci --prefer-offline
  else
    echo "  npm dependencies already installed (skip)"
  fi
}

# ── Web dev: deps only, no Next.js build ──────────────────────────────────────
build_web_dev() {
  echo "Preparing web (dev)..."
  build_web_deps
  green "  Web dev ready"
}

# ── Web prod: deps + `next build` + tsc server.ts ─────────────────────────────
build_web_prod() {
  echo "Building web (prod)..."
  build_web_deps
  echo "  Running npm run build (next build + tsc)..."
  npm --prefix "${WEB_DIR}" run build
  green "  Web prod artifacts ready (.next + server.js)"
}

# ── Docker ────────────────────────────────────────────────────────────────────
build_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    red "docker is not installed or not on PATH."
    exit 1
  fi
  echo "Building docker images..."
  # shellcheck disable=SC2046
  (cd "$DOCKER_DIR" && docker compose $(docker_compose_files) build)
  green "  Docker images built"
}

# ── Run ───────────────────────────────────────────────────────────────────────
bold "Building DevServer → ${MODE}"

case "$MODE" in
  dev)
    build_worker
    build_web_dev
    ;;
  prod)
    build_worker
    build_web_prod
    ;;
  docker)
    build_docker
    ;;
esac

echo ""
bold "Build complete (${MODE})"
case "$MODE" in
  dev)    echo "  Next: ./scripts/start.sh --dev" ;;
  prod)   echo "  Next: ./scripts/start.sh --prod" ;;
  docker) echo "  Next: ./scripts/start.sh --docker" ;;
esac
