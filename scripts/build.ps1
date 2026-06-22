# DevServer — build for dev, prod, or docker (Windows).
#
# Usage:
#   .\scripts\build.ps1            # dev (default)
#   .\scripts\build.ps1 -Dev       # worker venv + deps, web npm deps (no Next build)
#   .\scripts\build.ps1 -Prod      # worker venv + deps, web npm deps + Next.js build
#   .\scripts\build.ps1 -Docker    # docker compose build
#
# What "build" means per mode:
#   -Dev     Prepare the dev environment so `start.ps1 -Dev` runs instantly.
#            Creates the worker venv (uv or python -m venv), installs the worker
#            package editable, runs `npm ci` if node_modules is missing.
#            Skips `next build` — dev mode uses tsx hot reload.
#
#   -Prod    Everything -Dev does, PLUS `npm run build` (next build + tsc of
#            server.ts → server.js), producing the artifacts `start.ps1 -Prod` needs.
#
#   -Docker  `docker compose build` on the stack in docker\docker-compose.yml.
#
# This is the Windows counterpart to build.sh. The .sh script remains the source
# of truth on Linux/macOS; this never runs there.
[CmdletBinding()]
param(
    [switch]$Dev,
    [switch]$Prod,
    [switch]$Docker
)

. "$PSScriptRoot\_lib.ps1"

$Mode = 'dev'
if ($Prod)   { $Mode = 'prod' }
if ($Docker) { $Mode = 'docker' }

$VenvPy = Join-Path $WorkerDir '.venv\Scripts\python.exe'

# ── Worker venv + package install ─────────────────────────────────────────────
# Mirrors the logic in setup-local.ps1 / build.sh so behavior stays consistent.
function Build-Worker {
    Write-Host 'Building worker...'

    # Create venv if missing: prefer `uv venv` (project standard), else python -m venv.
    if (-not (Test-Path $VenvPy)) {
        Write-Host '  Creating Python venv...'
        if (Get-Command uv -ErrorAction SilentlyContinue) {
            Push-Location $WorkerDir
            try { & uv venv } finally { Pop-Location }
        } else {
            & python -m venv (Join-Path $WorkerDir '.venv')
        }
        if (-not (Test-Path $VenvPy)) {
            Write-Red 'Failed to create the worker venv.'
            exit 1
        }
    }

    # Install/refresh the worker package. Prefer uv (project standard); fall back
    # to the venv's `python -m pip`.
    Write-Host '  Installing worker package...'
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        Push-Location $WorkerDir
        try {
            $env:VIRTUAL_ENV = (Join-Path $WorkerDir '.venv')
            & uv pip install -q -e .
        } finally { Pop-Location }
    } else {
        & $VenvPy -m pip install -q -e $WorkerDir
    }
    if ($LASTEXITCODE -ne 0) { Write-Red '  Worker package install failed.'; exit 1 }

    Write-Green '  Worker ready (venv + package installed)'
}

# ── Web dependencies (npm ci) ─────────────────────────────────────────────────
function Build-WebDeps {
    if (-not (Test-Path (Join-Path $WebDir 'node_modules'))) {
        Write-Host '  Installing npm dependencies...'
        Push-Location $WebDir
        try {
            # Prefer a clean, reproducible install. `npm ci` requires the lockfile
            # to be in sync with package.json; if it has drifted, fall back to
            # `npm install` (which reconciles the lockfile) so the build still
            # succeeds on Windows instead of hard-failing.
            & npm ci --prefer-offline
            if ($LASTEXITCODE -ne 0) {
                Write-Yellow '  npm ci failed (lockfile out of sync) — falling back to npm install...'
                & npm install --prefer-offline
                if ($LASTEXITCODE -ne 0) { Write-Red '  npm install failed.'; exit 1 }
            }
        } finally { Pop-Location }
    } else {
        Write-Host '  npm dependencies already installed (skip)'
    }
}

# ── Web dev: deps only, no Next.js build ──────────────────────────────────────
function Build-WebDev {
    Write-Host 'Preparing web (dev)...'
    Build-WebDeps
    Write-Green '  Web dev ready'
}

# ── Web prod: deps + `next build` + tsc server.ts ─────────────────────────────
function Build-WebProd {
    Write-Host 'Building web (prod)...'
    Build-WebDeps
    Write-Host '  Running npm run build (next build + tsc)...'
    Push-Location $WebDir
    try { & npm run build } finally { Pop-Location }
    if ($LASTEXITCODE -ne 0) { Write-Red '  npm run build failed.'; exit 1 }
    Write-Green '  Web prod artifacts ready (.next + server.js)'
}

# ── Docker ────────────────────────────────────────────────────────────────────
function Build-Docker {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        Write-Red 'docker is not installed or not on PATH.'
        exit 1
    }
    # Default docker topology runs the worker on the host, so prepare its venv
    # too (the image only builds the web service in this mode).
    if (Test-HostWorker) {
        Write-Host 'Docker topology: worker on host — preparing host worker venv...'
        Build-Worker
    }
    Write-Host 'Building docker images...'
    Push-Location $DockerDir
    try {
        & docker compose @(Get-ComposeFiles) @(Get-ComposeProfiles) build
    } finally { Pop-Location }
    if ($LASTEXITCODE -ne 0) { Write-Red '  Docker build failed.'; exit 1 }
    Write-Green '  Docker images built'
}

# ── Run ───────────────────────────────────────────────────────────────────────
Write-Bold "Building DevServer -> $Mode"

switch ($Mode) {
    'dev' {
        Build-Worker
        Build-WebDev
    }
    'prod' {
        Build-Worker
        Build-WebProd
    }
    'docker' {
        Build-Docker
    }
}

Write-Host ''
Write-Bold "Build complete ($Mode)"
switch ($Mode) {
    'dev'    { Write-Host '  Next: .\scripts\start.ps1 -Dev' }
    'prod'   { Write-Host '  Next: .\scripts\start.ps1 -Prod' }
    'docker' { Write-Host '  Next: .\scripts\start.ps1 -Docker' }
}
