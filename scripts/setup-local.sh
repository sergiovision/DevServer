#!/bin/bash
# DevServer v2 — Local setup script
# Prerequisites: PostgreSQL 16+, Node.js 22+, Python 3.14+
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

# The worker's requires-python floor is 3.14 (apps/worker/pyproject.toml), but a
# distro's default `python3` is often older -- Ubuntu 24.04 ships 3.12 and keeps
# it as `python3` on purpose, since apt itself runs on it. Probe for a versioned
# interpreter first so an unmet floor fails here with an actionable message
# instead of surfacing as an opaque resolver error inside `uv pip install`.
PYTHON_MIN="3.14"

check_python() {
    local required candidate found
    required="$(echo "${PYTHON_MIN}" | tr '.' ',')"
    for candidate in "python${PYTHON_MIN}" python3 python; do
        command -v "$candidate" &>/dev/null || continue
        if "$candidate" -c "import sys; raise SystemExit(0 if sys.version_info[:2] >= (${required}) else 1)" 2>/dev/null; then
            echo "  ✓ python found: $(command -v "$candidate") ($("$candidate" -V 2>&1 | cut -d' ' -f2))"
            return 0
        fi
        found="$candidate"
    done
    echo "  ✗ Python ${PYTHON_MIN}+ not found. Please install Python ${PYTHON_MIN}+ (https://python.org)"
    if [[ -n "${found:-}" ]]; then
        echo "    (\`$found\` is $("$found" -V 2>&1 | cut -d' ' -f2), which is below the ${PYTHON_MIN} floor)"
        echo "    On Ubuntu/Debian: sudo add-apt-repository ppa:deadsnakes/ppa && sudo apt install python${PYTHON_MIN}-venv"
    fi
    return 1
}

check_cmd node "Node.js 22+ (https://nodejs.org)" || exit 1
check_python || exit 1
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
echo "  Dashboard: http://localhost:${WEB_PORT:-3200}"
echo "  Worker API: http://localhost:8000"
echo "═══════════════════════════════════════════"
