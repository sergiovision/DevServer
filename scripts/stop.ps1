# DevServer — stop processes for the given mode (and any orphans) on Windows.
#
# Usage:
#   .\scripts\stop.ps1            # dev (default): stop host processes + sweep orphans
#   .\scripts\stop.ps1 -Dev       # same
#   .\scripts\stop.ps1 -Prod      # same (dev/prod share WEB_PORT/WORKER_PORT)
#   .\scripts\stop.ps1 -Docker    # docker compose down (+ host worker)
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

switch ($Mode) {
    'docker' {
        if (-not (Invoke-DockerDown)) { Write-Red 'docker compose down failed'; exit 1 }
        # In the host-worker topology the worker runs on the host — stop it too.
        if (Test-HostWorker) {
            if (-not (Stop-DevServerHost)) { exit 1 }
        }
    }
    default {
        if (-not (Stop-DevServerHost)) { exit 1 }
    }
}
