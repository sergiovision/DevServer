# Shared helpers for DevServer start/stop/restart PowerShell scripts (Windows).
# Dot-source this file:  . "$PSScriptRoot\_lib.ps1"
#
# This is the Windows counterpart to _lib.sh. The .sh scripts remain the source
# of truth on Linux/macOS; these never run there and never modify them.

$ErrorActionPreference = 'Stop'

# ── Paths ─────────────────────────────────────────────────────────────────────
$ScriptDir = $PSScriptRoot
$Root      = Split-Path -Parent $ScriptDir
$DsLogDir  = Join-Path $Root 'logs'
$WorkerDir = Join-Path $Root 'apps\worker'
$WebDir    = Join-Path $Root 'apps\web'
$DockerDir = Join-Path $Root 'docker'

New-Item -ItemType Directory -Force -Path (Join-Path $DsLogDir 'tasks') | Out-Null

# ── Output ────────────────────────────────────────────────────────────────────
function Write-Red    { param($m) Write-Host $m -ForegroundColor Red }
function Write-Green  { param($m) Write-Host $m -ForegroundColor Green }
function Write-Yellow { param($m) Write-Host $m -ForegroundColor Yellow }
function Write-Bold   { param($m) Write-Host $m -ForegroundColor White }

# ── Env (.env loader) ─────────────────────────────────────────────────────────
# Mirrors `set -a; source .env`. Only ${VAR} references are expanded (the form
# used in .env.example for derived paths) — bare $VAR is left literal so values
# such as passwords are never mangled.
function Import-DotEnv {
    param([string]$Path)
    if (-not (Test-Path $Path)) { return }
    foreach ($line in Get-Content -LiteralPath $Path) {
        $t = $line.Trim()
        if ($t -eq '' -or $t.StartsWith('#')) { continue }
        $eq = $t.IndexOf('=')
        if ($eq -lt 1) { continue }
        $key = $t.Substring(0, $eq).Trim()
        $val = $t.Substring($eq + 1).Trim()
        if ($val.Length -ge 2 -and
            (($val[0] -eq '"' -and $val[-1] -eq '"') -or ($val[0] -eq "'" -and $val[-1] -eq "'"))) {
            $val = $val.Substring(1, $val.Length - 2)
        }
        $val = [regex]::Replace($val, '\$\{(\w+)\}', {
            param($m) [Environment]::GetEnvironmentVariable($m.Groups[1].Value) })
        Set-Item -Path "Env:$key" -Value $val
    }
}

Import-DotEnv (Join-Path $Root '.env')

$WorkerPort = if ($env:WORKER_PORT) { [int]$env:WORKER_PORT } else { 8000 }
$WebPort    = if ($env:WEB_PORT)    { [int]$env:WEB_PORT }
             elseif ($env:PORT)     { [int]$env:PORT }
             else                   { 3200 }
# Export so `npm run dev` / `node server.js` bind to it.
$env:WEB_PORT = "$WebPort"

# ── Mode parsing ──────────────────────────────────────────────────────────────
function Get-Mode {
    param([string[]]$ArgList)
    $mode = 'dev'
    foreach ($a in $ArgList) {
        switch ($a) {
            '--dev'    { $mode = 'dev' }
            '--prod'   { $mode = 'prod' }
            '--docker' { $mode = 'docker' }
        }
    }
    return $mode
}

# ── Process / port helpers ────────────────────────────────────────────────────
function Test-PortBusy {
    param([int]$Port)
    $null -ne (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1)
}

function Get-PortPids {
    param([int]$Port)
    Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique
}

# All descendant PIDs of $RootPid (children, grandchildren, …). Walks the
# recorded ParentProcessId graph, which Windows keeps pointing at the original
# parent even after that parent dies — so orphaned grandchildren (e.g. a
# uvicorn --reload / multiprocessing.spawn worker whose reloader was killed)
# are still discoverable and killable.
function Get-DescendantPids {
    param([int]$RootPid)
    $procs = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Select-Object ProcessId, ParentProcessId
    $found = [System.Collections.Generic.List[int]]::new()
    $stack = [System.Collections.Generic.Stack[int]]::new()
    $stack.Push($RootPid)
    while ($stack.Count -gt 0) {
        $cur = $stack.Pop()
        foreach ($p in $procs) {
            if ($p.ParentProcessId -eq $cur -and -not $found.Contains([int]$p.ProcessId)) {
                $found.Add([int]$p.ProcessId)
                $stack.Push([int]$p.ProcessId)
            }
        }
    }
    return $found
}

# Kill a process and its entire descendant tree. Descendants are collected
# first (and survive the root's death via recorded ParentProcessId), so a
# stale/dead root still gets its live children reaped — which is what frees a
# listening socket the OwningProcess column attributes to a dead PID.
function Stop-ProcessTree {
    param([int]$RootPid)
    $targets = @(Get-DescendantPids $RootPid)
    $targets += $RootPid
    foreach ($procId in ($targets | Select-Object -Unique)) {
        try { Stop-Process -Id $procId -Force -ErrorAction Stop } catch {}
    }
}

function Stop-Port {
    param([int]$Port)
    foreach ($procId in (Get-PortPids $Port)) {
        Stop-ProcessTree -RootPid $procId
    }
}

# Launch a detached, hidden background process whose merged stdout+stderr is
# appended to $LogFile — the Windows analogue of `nohup … >> log 2>&1 &`.
# Returns the launcher PID. Uses `cmd /s /c "…"` so the redirection merge works
# and the quoting around the exe path / log path survives intact.
function Start-Detached {
    param(
        [string]$Inner,        # full command incl. quoted exe + args (no redirection)
        [string]$WorkDir,
        [string]$LogFile
    )
    $full = '/s /c "{0} >> "{1}" 2>&1"' -f $Inner, $LogFile
    $p = Start-Process -FilePath $env:ComSpec -ArgumentList $full `
        -WorkingDirectory $WorkDir -WindowStyle Hidden -PassThru
    return $p.Id
}

# Sweep stray devserver processes spawned from the project tree (orphans whose
# parent shell is gone). Matches the worker venv / web node_modules in the
# command line, excludes our own PID.
function Invoke-SweepOrphans {
    $procs = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.CommandLine -and $_.ProcessId -ne $PID -and
        ($_.CommandLine -like "*$WorkerDir*" -or $_.CommandLine -like "*$WebDir*")
    }
    foreach ($p in $procs) {
        try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop } catch {}
    }
}

# ── Docker helpers ────────────────────────────────────────────────────────────
# True only when the docker CLI is installed and on PATH. Used to make dev/prod
# host modes work on machines without Docker — the docker-aware helpers below
# all short-circuit through this instead of letting `& docker` throw a
# CommandNotFoundException (this lib sets $ErrorActionPreference = 'Stop').
function Test-DockerAvailable {
    return [bool](Get-Command docker -ErrorAction SilentlyContinue)
}

function Test-DockerRunning {
    if (-not (Test-DockerAvailable)) { return $false }
    # docker.exe can be installed while the daemon is down (Docker Desktop not
    # started). `docker ps` then writes to stderr, which PowerShell 5.1 promotes
    # to a terminating NativeCommandError under this lib's 'Stop' preference.
    # Locally relax the preference + try/catch so a stopped daemon reads as
    # "not running" instead of aborting dev/prod host startup.
    $names = $null
    try {
        $ErrorActionPreference = 'SilentlyContinue'
        $names = & docker ps --format '{{.Names}}' 2>$null
    } catch { return $false }
    return [bool]($names | Where-Object { $_ -match '^devserver-(web|worker|postgres)$' })
}

# True unless DEVSERVER_COMPOSE=all-in-docker. host-worker is the default.
function Test-HostWorker {
    return ($env:DEVSERVER_COMPOSE -ne 'all-in-docker')
}

# Windows always uses the bundled DB unless explicitly opted out (no host-PG
# autodetect — Windows has no peer-auth postgres convention).
function Test-UseHostDb {
    switch ("$($env:DEVSERVER_HOST_DB)".ToLower()) {
        { $_ -in '1','true','yes' } { return $true }
        default                     { return $false }
    }
}

function Test-UseBundledDb {
    if (Test-HostWorker) { return -not (Test-UseHostDb) }
    return -not (Test-UseHostDb)
}

# Returns the `-f <file>` argument array for `docker compose`.
function Get-ComposeFiles {
    if (Test-HostWorker) {
        return @('-f', (Join-Path $DockerDir 'docker-compose.host-worker.yml'))
    }
    $files = @('-f', (Join-Path $DockerDir 'docker-compose.yml'))
    $hostDb = Join-Path $DockerDir 'docker-compose.host-db.yml'
    if ((Test-Path $hostDb) -and (Test-UseHostDb)) { $files += @('-f', $hostDb) }
    return $files
}

function Get-ComposeProfiles {
    if (Test-UseBundledDb) { return @('--profile', 'bundled-db') }
    return @()
}

function Invoke-DockerDown {
    if (Test-DockerRunning) {
        Write-Host 'Stopping docker stack...'
        Push-Location $DockerDir
        try {
            & docker compose @(Get-ComposeFiles) --profile bundled-db down
            if ($LASTEXITCODE -ne 0) { return $false }
        } finally { Pop-Location }
        Write-Green '  Docker stack stopped'
    }
    return $true
}

# ── Host stop ─────────────────────────────────────────────────────────────────
function Stop-DevServerHost {
    $stopped = $false

    foreach ($pair in @(@('Worker', (Join-Path $DsLogDir 'worker.pid')),
                         @('Web',    (Join-Path $DsLogDir 'web.pid')))) {
        $name = $pair[0]; $pidFile = $pair[1]
        if (Test-Path $pidFile) {
            $procId = (Get-Content -LiteralPath $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
            if ($procId -and (Get-Process -Id $procId -ErrorAction SilentlyContinue)) {
                # Kill the whole tree: the pid file holds the cmd launcher, whose
                # python/node grandchildren actually bind the ports.
                Stop-ProcessTree -RootPid ([int]$procId)
                Write-Green "  Stopped $name (pid $procId)"
                $stopped = $true
            }
            Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
        }
    }

    foreach ($port in @($WorkerPort, $WebPort)) {
        if (Test-PortBusy $port) { Stop-Port $port; $stopped = $true }
    }
    Invoke-SweepOrphans

    # Poll for the kernel to release the listening sockets, re-killing the
    # current owner's tree each pass. A force-killed uvicorn --reload child can
    # briefly retain an inherited socket after its owner dies; a fixed 1s wait
    # raced that and falsely reported failure.
    $deadline   = (Get-Date).AddSeconds(10)
    $stillBusy  = $false
    do {
        $stillBusy = $false
        foreach ($port in @($WorkerPort, $WebPort)) {
            if (Test-PortBusy $port) {
                $stillBusy = $true
                Stop-Port $port
            }
        }
        if ($stillBusy) { Start-Sleep -Milliseconds 500 }
    } while ($stillBusy -and (Get-Date) -lt $deadline)

    if ($stillBusy) {
        Write-Red "Failed to free ports $WorkerPort/$WebPort."
        return $false
    }
    if ($stopped) { Write-Green 'Host processes stopped' }
    else          { Write-Host 'No host processes were running' }
    return $true
}
