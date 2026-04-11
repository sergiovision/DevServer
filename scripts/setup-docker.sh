#!/bin/bash
# DevServer v2 — Docker setup script
# Prerequisites: Docker, Docker Compose
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
DOCKER_DIR="${PROJECT_ROOT}/docker"

echo "═══════════════════════════════════════════"
echo "  DevServer v2 — Docker Setup"
echo "═══════════════════════════════════════════"

# --- Check prerequisites ---
echo ""
echo "Checking prerequisites..."

if ! command -v docker &>/dev/null; then
    echo "  ✗ Docker not found. Install: https://docs.docker.com/get-docker/"
    exit 1
fi
echo "  ✓ docker: $(docker --version)"

if ! docker compose version &>/dev/null; then
    echo "  ✗ Docker Compose not found."
    exit 1
fi
echo "  ✓ docker compose: $(docker compose version --short)"

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

# --- Choose mode ---
echo ""
echo "Choose deployment mode:"
echo "  1) Full Docker (PostgreSQL + Valkey + Web + Worker)"
echo "  2) Local PostgreSQL (Valkey + Web + Worker in Docker, PG on host)"
echo ""
read -rp "Choice [1]: " MODE_CHOICE
MODE_CHOICE="${MODE_CHOICE:-1}"

cd "${DOCKER_DIR}"

# Symlink .env for Docker Compose
ln -sf "${PROJECT_ROOT}/.env" "${DOCKER_DIR}/.env"

case "$MODE_CHOICE" in
    1)
        echo ""
        echo "Starting full Docker stack..."
        docker compose up -d --build
        ;;
    2)
        echo ""
        echo "Starting Docker stack with local PostgreSQL..."
        echo "  Make sure PostgreSQL is running on host and migrations are applied."
        docker compose -f docker-compose.yml -f docker-compose.local.yml up -d --build
        ;;
    *)
        echo "Invalid choice"
        exit 1
        ;;
esac

echo ""
echo "═══════════════════════════════════════════"
echo "  Docker stack is starting!"
echo ""
echo "  Dashboard:  http://localhost:3000"
echo "  Worker API: http://localhost:8000"
echo ""
echo "  Logs: docker compose logs -f"
echo "  Stop: docker compose down"
echo "═══════════════════════════════════════════"
