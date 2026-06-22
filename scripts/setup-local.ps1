# DevServer v2 — Local setup script (Windows).
# Prerequisites: PostgreSQL 16+, Node.js 22+, Python 3.12+
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$ScriptDir   = $PSScriptRoot
$ProjectRoot = Split-Path -Parent $ScriptDir

. "$PSScriptRoot\_lib.ps1" *> $null  # Write-* helpers + Import-DotEnv (.env is loaded later, after we ensure it exists)

Write-Host '==========================================='
Write-Host '  DevServer v2 - Local Setup (Windows)'
Write-Host '==========================================='

# --- Check prerequisites ---
Write-Host ''
Write-Host 'Checking prerequisites...'

function Test-Cmd {
    param([string]$Name, [string]$Hint, [switch]$Optional)
    $c = Get-Command $Name -ErrorAction SilentlyContinue
    if (-not $c) {
        Write-Host "  x $Name not found. Please install $Hint"
        if (-not $Optional) { exit 1 }
        return
    }
    Write-Host "  ok $Name found: $($c.Source)"
}

Test-Cmd node   'Node.js 22+ (https://nodejs.org)'
Test-Cmd python 'Python 3.12+ (https://python.org)'
Test-Cmd psql   'PostgreSQL 16+ (https://postgresql.org)'
Test-Cmd claude 'Claude Code CLI (npm install -g @anthropic-ai/claude-code)' -Optional

# --- Environment ---
Write-Host ''
$envPath = Join-Path $ProjectRoot '.env'
if (-not (Test-Path $envPath)) {
    Write-Host 'Creating .env from template...'
    Copy-Item (Join-Path $ProjectRoot 'config\.env.example') $envPath
    Write-Host '  !  Edit .env with your credentials before continuing!'
    Write-Host "     $envPath"
    exit 0
}
Write-Host '  ok .env exists'

# --- Database ---
# First-run bootstrap: create the app role + database before migrations run.
# Fixes `password authentication failed for user "devserver"` on a fresh host
# PostgreSQL — the app role/database don't exist yet (or the role's password has
# drifted from .env). We connect as a superuser, create the role + database
# idempotently and sync the password. (Docker mode never reaches here — the
# bundled postgres image creates the devserver superuser on first init.)
. "$PSScriptRoot\_lib.ps1" *> $null   # reload .env now that it's guaranteed to exist

$PgHost     = if ($env:PGHOST)     { $env:PGHOST }     else { '127.0.0.1' }
$PgPort     = if ($env:PGPORT)     { $env:PGPORT }     else { '5432' }
$PgUser     = if ($env:PGUSER)     { $env:PGUSER }     else { 'devserver' }
$PgDatabase = if ($env:PGDATABASE) { $env:PGDATABASE } else { 'devserver' }
$PgPassword = if ($env:PGPASSWORD) { $env:PGPASSWORD } else { '' }
$SuperUser  = if ($env:PGSUPERUSER)     { $env:PGSUPERUSER }     else { 'postgres' }
$SuperPw    = if ($env:PGSUPERPASSWORD) { $env:PGSUPERPASSWORD } else { '' }

# Quietly probe whether a login succeeds. Runs psql under a relaxed error
# preference so a failed auth (native stderr) reads as a non-zero exit code
# instead of a terminating NativeCommandError under this script's 'Stop' pref.
function Test-PgLogin {
    param([string]$User, [string]$Pw, [string]$Db)
    $env:PGPASSWORD = $Pw
    try {
        $ErrorActionPreference = 'SilentlyContinue'
        & psql -h $PgHost -p $PgPort -U $User -d $Db -tAc 'SELECT 1' *> $null
        return ($LASTEXITCODE -eq 0)
    } catch { return $false }
}

# Run psql as the resolved superuser against $Db. Errors stay visible (psql
# prints them) but don't abort the script — callers gate on $LASTEXITCODE.
function Invoke-PsqlSuper {
    param([string]$Db, [string[]]$PsqlArgs)
    $env:PGPASSWORD = $SuperPw
    $ErrorActionPreference = 'Continue'
    & psql -h $PgHost -p $PgPort -U $SuperUser -d $Db @PsqlArgs
}

Write-Host ''
Write-Host 'Setting up the database...'

# Fast path: the app user can already log in to its own database.
if (Test-PgLogin -User $PgUser -Pw $PgPassword -Db $PgDatabase) {
    Write-Host "  ok '$PgUser' can already log in to '$PgDatabase'"
} else {
    Write-Yellow "  Database not reachable as '$PgUser' - bootstrapping as superuser '$SuperUser'..."

    # Make sure we can reach Postgres as the superuser; prompt if no/blank password works.
    if (-not (Test-PgLogin -User $SuperUser -Pw $SuperPw -Db 'postgres')) {
        $sec = Read-Host "  Password for PostgreSQL superuser '$SuperUser'" -AsSecureString
        $SuperPw = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
            [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
        if (-not (Test-PgLogin -User $SuperUser -Pw $SuperPw -Db 'postgres')) {
            Write-Red "  Cannot connect to PostgreSQL as superuser '$SuperUser' at ${PgHost}:$PgPort."
            Write-Yellow '  Set PGSUPERUSER / PGSUPERPASSWORD in .env (or the environment), or create the role+db manually:'
            Write-Yellow "    CREATE ROLE `"$PgUser`" WITH LOGIN SUPERUSER PASSWORD '...'; CREATE DATABASE `"$PgDatabase`" OWNER `"$PgUser`";"
            exit 1
        }
    }

    $pwEsc = $PgPassword -replace "'", "''"   # single-quote-escape for SQL literal

    # Role: create if missing, else sync its password to .env so an existing
    # role with a stale password stops failing authentication. Created as
    # SUPERUSER to match the Docker bundled role (migrations CREATE EXTENSION
    # vector, and the backup/restore tooling assumes admin rights).
    $roleExists = (Invoke-PsqlSuper -Db 'postgres' -PsqlArgs @('-tAc', "SELECT 1 FROM pg_roles WHERE rolname = '$PgUser'") | Out-String).Trim()
    if ($roleExists -eq '1') {
        Invoke-PsqlSuper -Db 'postgres' -PsqlArgs @('-v', 'ON_ERROR_STOP=1', '-c', "ALTER ROLE `"$PgUser`" WITH LOGIN PASSWORD '$pwEsc'") | Out-Null
        if ($LASTEXITCODE -ne 0) { Write-Red "  failed to update role '$PgUser'"; exit 1 }
        Write-Host "  role '$PgUser' already existed - password synced to .env"
    } else {
        Invoke-PsqlSuper -Db 'postgres' -PsqlArgs @('-v', 'ON_ERROR_STOP=1', '-c', "CREATE ROLE `"$PgUser`" WITH LOGIN SUPERUSER PASSWORD '$pwEsc'") | Out-Null
        if ($LASTEXITCODE -ne 0) { Write-Red "  failed to create role '$PgUser'"; exit 1 }
        Write-Green "  created role '$PgUser'"
    }

    # Database: CREATE DATABASE can't run inside a transaction/DO block, so gate
    # it with a plain existence check.
    $dbExists = (Invoke-PsqlSuper -Db 'postgres' -PsqlArgs @('-tAc', "SELECT 1 FROM pg_database WHERE datname = '$PgDatabase'") | Out-String).Trim()
    if ($dbExists -ne '1') {
        Invoke-PsqlSuper -Db 'postgres' -PsqlArgs @('-v', 'ON_ERROR_STOP=1', '-c', "CREATE DATABASE `"$PgDatabase`" OWNER `"$PgUser`"") | Out-Null
        if ($LASTEXITCODE -ne 0) { Write-Red "  failed to create database '$PgDatabase'"; exit 1 }
        Write-Green "  created database '$PgDatabase' (owner '$PgUser')"
    }

    # Final check: the app user must now be able to log in, or migrations fail
    # the same way again.
    if (-not (Test-PgLogin -User $PgUser -Pw $PgPassword -Db $PgDatabase)) {
        Write-Red "  Bootstrap ran but '$PgUser' still cannot log in to '$PgDatabase'."
        exit 1
    }
    Write-Green '  Database bootstrap complete.'
}

Write-Host ''
Write-Host 'Running database migrations...'
& (Join-Path $ScriptDir 'migrate.ps1') local

# --- Python worker ---
Write-Host ''
Write-Host 'Setting up Python worker...'
$workerDir = Join-Path $ProjectRoot 'apps\worker'
$venvPy    = Join-Path $workerDir '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPy)) {
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        Push-Location $workerDir; try { & uv venv } finally { Pop-Location }
    } else {
        & python -m venv (Join-Path $workerDir '.venv')
    }
}
if (Get-Command uv -ErrorAction SilentlyContinue) {
    Push-Location $workerDir
    try { $env:VIRTUAL_ENV = (Join-Path $workerDir '.venv'); & uv pip install -q -e . }
    finally { Pop-Location }
} else {
    & $venvPy -m pip install -e $workerDir --quiet
}
Write-Host '  ok Python worker dependencies installed'

# --- Next.js web ---
Write-Host ''
Write-Host 'Setting up Next.js web app...'
Push-Location (Join-Path $ProjectRoot 'apps\web')
try { & npm install --prefer-offline } finally { Pop-Location }
Write-Host '  ok Web app dependencies installed'

# --- Runtime directories ---
New-Item -ItemType Directory -Force -Path (Join-Path $ProjectRoot 'worktrees') | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $ProjectRoot 'logs\tasks') | Out-Null

$webPort = if ($env:WEB_PORT) { $env:WEB_PORT } else { '3200' }
Write-Host ''
Write-Host '==========================================='
Write-Host '  Setup complete!'
Write-Host ''
Write-Host '  Start everything (worker + web):'
Write-Host '    .\scripts\start.ps1'
Write-Host ''
Write-Host "  Dashboard:  http://localhost:$webPort"
Write-Host '  Worker API: http://localhost:8000'
Write-Host '==========================================='
