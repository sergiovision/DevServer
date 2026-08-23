# DevServer — start in dev (default), prod, or docker mode (Windows).
#
# Usage:
#   .\scripts\start.ps1            # dev mode (hot reload)
#   .\scripts\start.ps1 -Dev       # dev mode (hot reload)
#   .\scripts\start.ps1 -Prod      # prod mode (build + node server.js)
#   .\scripts\start.ps1 -Docker    # docker compose up -d --build
#
# Logs: logs\worker.log, logs\web.log   PIDs: logs\worker.pid, logs\web.pid
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

$WorkerLog = Join-Path $DsLogDir 'worker.log'
$WebLog    = Join-Path $DsLogDir 'web.log'
$VenvPy    = Join-Path $WorkerDir '.venv\Scripts\python.exe'

function Invoke-Preflight {
    if ($Mode -eq 'docker') {
        if (-not (Test-DockerAvailable)) {
            Write-Red 'Docker is not installed or not on PATH.'
            Write-Yellow '  Run host mode instead: .\scripts\start.ps1 -Dev   (or -Prod)'
            exit 1
        }
        if ((Test-PortBusy $WebPort) -or (Test-PortBusy $WorkerPort)) {
            Write-Red "Ports $WebPort/$WorkerPort are in use by host processes."
            Write-Yellow '  Run: .\scripts\stop.ps1   (then try again)'
            exit 1
        }
    } else {
        if (Test-DockerRunning) {
            Write-Red 'DevServer docker stack is already running.'
            Write-Yellow "  Run: .\scripts\restart.ps1 -$Mode   (switches modes cleanly)"
            exit 1
        }
        if ((Test-PortBusy $WebPort) -or (Test-PortBusy $WorkerPort)) {
            Write-Red "Ports $WebPort/$WorkerPort are already in use."
            Write-Yellow '  Run: .\scripts\stop.ps1   (then try again)'
            exit 1
        }
    }
}

function Start-Worker {
    Write-Host "Starting worker ($Mode)..."

    if (-not (Test-Path $VenvPy)) {
        Write-Host '  Creating Python venv...'
        if (Get-Command uv -ErrorAction SilentlyContinue) {
            Push-Location $WorkerDir; try { & uv venv } finally { Pop-Location }
        } else {
            & python -m venv (Join-Path $WorkerDir '.venv')
        }
    }

    # Install/refresh the worker package. Prefer uv; fall back to the venv's pip.
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        Push-Location $WorkerDir
        try { $env:VIRTUAL_ENV = (Join-Path $WorkerDir '.venv'); & uv pip install -q -e . }
        finally { Pop-Location }
    } else {
        & $VenvPy -m pip install -q -e $WorkerDir
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Red '  Worker dependency installation failed; worker was not started.'
        exit 1
    }

    # A cancelled package update can leave dist-info behind while package
    # files are incomplete. Catch that state before uvicorn starts with the
    # entire Pro router silently unavailable.
    & $VenvPy -c 'from cryptography.exceptions import InvalidSignature'
    if ($LASTEXITCODE -ne 0) {
        Write-Red '  Worker dependency verification failed; recreate or repair apps\worker\.venv.'
        exit 1
    }

    $reload = ''
    if ($Mode -eq 'dev') { $reload = ' --reload' }

    $env:PYTHONPATH = (Join-Path $WorkerDir 'src')
    $inner = '"{0}" -m uvicorn src.main:app --host 0.0.0.0 --port {1} --app-dir "{2}"{3}' -f `
        $VenvPy, $WorkerPort, $WorkerDir, $reload
    $procId = Start-Detached -Inner $inner -WorkDir $WorkerDir -LogFile $WorkerLog
    Set-Content -LiteralPath (Join-Path $DsLogDir 'worker.pid') -Value $procId
    Write-Green "  Worker started (pid $procId) -> logs\worker.log"
}

function Start-Web {
    Write-Host "Starting web ($Mode)..."

    if (-not (Test-Path (Join-Path $WebDir 'node_modules'))) {
        Write-Host '  Installing npm dependencies...'
        & npm --prefix $WebDir ci --prefer-offline
    }

    if ($Mode -eq 'prod') {
        Write-Host '  Building Next.js...'
        & npm --prefix $WebDir run build
        $env:NODE_ENV = 'production'
        $procId = Start-Detached -Inner 'node server.js' -WorkDir $WebDir -LogFile $WebLog
    } else {
        $procId = Start-Detached -Inner 'npm run dev' -WorkDir $WebDir -LogFile $WebLog
    }
    Set-Content -LiteralPath (Join-Path $DsLogDir 'web.pid') -Value $procId
    Write-Green "  Web started (pid $procId) -> logs\web.log"
}

function Start-DockerStack {
    Write-Host 'Starting docker stack...'
    if (Test-HostWorker) {
        Write-Host '  Topology: worker on host (Postgres + web in Docker)'
    }
    if (Test-UseBundledDb) {
        Write-Host '  Database: bundled PostgreSQL container'
    } else {
        Write-Yellow '  Database: external / host PostgreSQL (bundled container skipped)'
    }
    Push-Location $DockerDir
    try {
        & docker compose @(Get-ComposeFiles) @(Get-ComposeProfiles) up -d --build
    } finally { Pop-Location }
    Write-Green '  Docker stack up'

    if (Test-HostWorker) {
        Set-Content -LiteralPath $WorkerLog -Value '' -NoNewline
        Start-Worker
    }
}

# ── Run ───────────────────────────────────────────────────────────────────────
Invoke-Preflight

switch ($Mode) {
    { $_ -in 'dev', 'prod' } {
        Set-Content -LiteralPath $WorkerLog -Value '' -NoNewline
        Set-Content -LiteralPath $WebLog    -Value '' -NoNewline
        Start-Worker
        Start-Web
    }
    'docker' { Start-DockerStack }
}

Write-Host ''
Write-Bold "DevServer running ($Mode)"
Write-Host "  Dashboard:   http://localhost:$WebPort"
Write-Host "  Worker API:  http://localhost:$WorkerPort"
if ($Mode -eq 'docker') {
    if (Test-HostWorker) {
        Write-Host '  Worker log:  Get-Content logs\worker.log -Wait  (worker runs on host)'
    }
} else {
    Write-Host '  Worker log:  Get-Content logs\worker.log -Wait'
    Write-Host '  Web log:     Get-Content logs\web.log -Wait'
}
Write-Host "  Stop:        .\scripts\stop.ps1 -$Mode"
