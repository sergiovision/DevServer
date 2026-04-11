#!/bin/bash
# DevServer v2 — Local setup script
# Prerequisites: PostgreSQL 16+, Node.js 22+, Python 3.12+
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

echo "═══════════════════════════════════════════"
echo "  DevServer v2 — Local Setup"
echo "═══════════════════════════════════════════"

# --- Check prerequisites ---
echo ""
echo "Checking prerequisites..."

check_cmd() {
    if ! command -v "$1" &>/dev/null; then
        echo "  ✗ $1 not found. Please install $2"
        return 1
    fi
    echo "  ✓ $1 found: $(command -v "$1")"
}

check_cmd node "Node.js 22+ (https://nodejs.org)" || exit 1
check_cmd python3 "Python 3.12+ (https://python.org)" || exit 1
check_cmd psql "PostgreSQL 16+ (https://postgresql.org)" || exit 1
check_cmd claude "Claude Code CLI (npm install -g @anthropic-ai/claude-code)" || true

# --- Environment ---
echo ""
if [[ ! -f "${PROJECT_ROOT}/.env" ]]; then
    echo "Creating .env from template..."
    cp "${PROJECT_ROOT}/config/.env.example" "${PROJECT_ROOT}/.env"
    echo "  ⚠  Edit .env with your credentials before continuing!"
    echo "     ${PROJECT_ROOT}/.env"
    exit 0
fi
echo "  ✓ .env exists"

# Load env
set -a
source "${PROJECT_ROOT}/.env"
set +a

# --- Database ---
echo ""
echo "Running database migrations..."
"${SCRIPT_DIR}/migrate.sh" local

# --- Python worker ---
echo ""
echo "Setting up Python worker..."
cd "${PROJECT_ROOT}/apps/worker"
if [[ ! -d ".venv" ]]; then
    python3 -m venv .venv
fi
source .venv/bin/activate
pip install -e . --quiet
deactivate
echo "  ✓ Python worker dependencies installed"

# --- Next.js web ---
echo ""
echo "Setting up Next.js web app..."
cd "${PROJECT_ROOT}/apps/web"
npm install --prefer-offline
echo "  ✓ Web app dependencies installed"

# --- Runtime directories ---
mkdir -p "${PROJECT_ROOT}/worktrees" "${PROJECT_ROOT}/logs/tasks"

echo ""
echo "═══════════════════════════════════════════"
echo "  Setup complete!"
echo ""
echo "  Start the worker:"
echo "    cd apps/worker && source .venv/bin/activate"
echo "    PYTHONPATH=src uvicorn src.main:app --host 0.0.0.0 --port 8000"
echo ""
echo "  Start the web dashboard:"
echo "    cd apps/web && npm run dev"
echo ""
echo "  Dashboard: http://localhost:3000"
echo "  Worker API: http://localhost:8000"
echo "═══════════════════════════════════════════"
