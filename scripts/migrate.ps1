# Run database migrations (Windows).
# Usage: .\scripts\migrate.ps1 [local|docker]
[CmdletBinding()]
param(
    [ValidateSet('local', 'docker')]
    [string]$Mode = 'local'
)

$ErrorActionPreference = 'Stop'
$ScriptDir     = $PSScriptRoot
$ProjectRoot   = Split-Path -Parent $ScriptDir
$MigrationsDir = Join-Path $ProjectRoot 'database\migrations'

. "$PSScriptRoot\_lib.ps1" *> $null  # reuse Import-DotEnv (loads .env into env)

$PgHost     = if ($env:PGHOST)     { $env:PGHOST }     else { '127.0.0.1' }
$PgPort     = if ($env:PGPORT)     { $env:PGPORT }     else { '5432' }
$PgUser     = if ($env:PGUSER)     { $env:PGUSER }     else { 'devserver' }
$PgDatabase = if ($env:PGDATABASE) { $env:PGDATABASE } else { 'devserver' }
$PgPassword = if ($env:PGPASSWORD) { $env:PGPASSWORD } else { '' }

function Invoke-Psql {
    param([string[]]$PsqlArgs, [string]$InputFile)
    # psql writes NOTICEs ("extension already exists, skipping", etc.) to stderr.
    # Under this script's 'Stop' preference, PowerShell 5.1 promotes ANY native
    # stderr line to a terminating NativeCommandError — which aborts an otherwise
    # successful migration on its first NOTICE. Run psql under 'Continue' and let
    # ON_ERROR_STOP=1 surface real SQL failures through $LASTEXITCODE (which the
    # callers already gate on); PGOPTIONS mutes NOTICEs so stderr stays clean.
    $ErrorActionPreference = 'Continue'
    if ($Mode -eq 'docker') {
        $base = @('exec', '-i', 'devserver-postgres', 'psql', '-v', 'ON_ERROR_STOP=1', '-U', $PgUser, '-d', $PgDatabase) + $PsqlArgs
        if ($InputFile) {
            Get-Content -LiteralPath $InputFile -Raw | & docker @base
        } else {
            & docker @base
        }
    } else {
        $env:PGPASSWORD = $PgPassword
        $env:PGOPTIONS  = '-c client_min_messages=warning'
        $base = @('-h', $PgHost, '-p', $PgPort, '-v', 'ON_ERROR_STOP=1', '-U', $PgUser, '-d', $PgDatabase) + $PsqlArgs
        if ($InputFile) { $base += @('-f', $InputFile) }
        & psql @base
    }
}

# Pre-flight: refuse to run if an idle-in-transaction session holds locks on tasks.
function Test-Blockers {
    $sql = @"
SELECT string_agg(pid::text || ' (' || state || ')', ', ')
FROM pg_stat_activity
WHERE datname = 'devserver'
  AND pid <> pg_backend_pid()
  AND state IN ('idle in transaction', 'idle in transaction (aborted)')
  AND query ILIKE '%tasks%';
"@
    $blockers = ''
    try { $blockers = (Invoke-Psql -PsqlArgs @('-tAc', $sql) 2>$null | Out-String).Trim() } catch {}
    if ($blockers) {
        Write-Red 'Migration blocked: idle-in-transaction sessions hold locks on tasks:'
        Write-Red "  $blockers"
        Write-Yellow '  Terminate them, then retry (SELECT pg_terminate_backend(pid) ...).'
        exit 1
    }
}

Write-Host "Running migrations (mode: $Mode)..."
Test-Blockers

Get-ChildItem -LiteralPath $MigrationsDir -Filter '*.sql' | Sort-Object Name | ForEach-Object {
    Write-Host "  -> $($_.Name)"
    Invoke-Psql -InputFile $_.FullName
    if ($LASTEXITCODE -ne 0) { Write-Red "  migration failed: $($_.Name)"; exit 1 }
}

Write-Host 'Migrations complete.'
