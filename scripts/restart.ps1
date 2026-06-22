# DevServer — clean mode-switch restart (Windows).
#
# Tears down EVERYTHING (host processes + docker stack + orphans) before
# starting fresh in the requested mode. Use this to switch dev / prod / docker.
#
# Usage:
#   .\scripts\restart.ps1            # dev (default)
#   .\scripts\restart.ps1 -Dev
#   .\scripts\restart.ps1 -Prod
#   .\scripts\restart.ps1 -Docker
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

Write-Bold "Restarting DevServer -> $Mode"

# 1. Tear down docker if running (no-op if not).
if (-not (Invoke-DockerDown)) { Write-Red 'Failed to stop docker stack'; exit 1 }

# 2. Tear down all host processes + orphans.
if (-not (Stop-DevServerHost)) { Write-Red 'Failed to stop host processes - see ports above'; exit 1 }

# 3. Hand off to start.ps1 in the requested mode. Forward the bound switch
#    (-Dev/-Prod/-Docker) verbatim via splatting; a stringified "-$Mode" passes
#    as a positional value and fails to bind to start.ps1's [switch] params.
#    No switch (default) → start.ps1's own default (dev).
& (Join-Path $PSScriptRoot 'start.ps1') @PSBoundParameters
exit $LASTEXITCODE
