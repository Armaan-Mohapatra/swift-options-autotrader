#Requires -Version 5.1
<#
.SYNOPSIS
  Install / inspect / remove the EXPERIMENTAL review-only Swift cloud bridge.

.DESCRIPTION
  The bridge reads the installed Swift SQLite database read-only and fires a Claude Code cloud
  scheduled task (API trigger) for each fresh parsed decision. It never places or cancels orders,
  never changes PAPER_ONLY / LIVE_ENABLED / ALLOW_BROKER_WRITES, and never touches Phoenix/Kalshi.

  Actions:
    -Action Plan       print what Install would do (default; changes nothing)
    -Action Install    write config, enroll the API token with DPAPI, register a logon task
    -Action Status     print the bridge heartbeat
    -Action Pause      create bridge.paused (stops all fires immediately)
    -Action Resume     remove bridge.paused
    -Action Uninstall  unregister the task; keeps the outbox for audit (rollback)

.EXAMPLE
  .\cloud-bridge.ps1 -Action Plan -FireUrl https://api.anthropic.com/v1/claude_code/routines/trig_XXX/fire
#>
[CmdletBinding()]
param(
    [ValidateSet('Plan', 'Install', 'Status', 'Pause', 'Resume', 'Uninstall')]
    [string]$Action = 'Plan',
    [string]$InstallRoot = '',
    [ValidateSet('Paper', 'Live')]
    [string]$Mode = 'Paper',
    [string]$FireUrl = '',
    [string]$TaskName = 'SwiftCloudBridgeReviewOnly'
)
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

Assert-SwiftWindows
$root = Resolve-SwiftInstallRoot $InstallRoot
$dbName = if ($Mode -eq 'Live') { 'live.sqlite3' } else { 'swift.sqlite3' }
$liveDb = Join-Path $root "data\$dbName"
$bridgeDir = Join-Path $root 'cloud-bridge'
$configPath = Join-Path $bridgeDir 'bridge-config.json'

function Invoke-Bridge([string[]]$BridgeArgs) {
    $python = Get-SwiftPython $root
    & $python -I -m swift_autotrader.cloud_bridge --config $configPath @BridgeArgs
    if ($LASTEXITCODE -ne 0) { throw "bridge command failed ($LASTEXITCODE)" }
}

switch ($Action) {
    'Plan' {
        Write-Output "InstallRoot      : $root"
        Write-Output "Swift database   : $liveDb (opened read-only)"
        Write-Output "Bridge directory : $bridgeDir (own outbox, heartbeat, DPAPI token)"
        Write-Output "Scheduled task   : $TaskName (at logon, current user, restart on failure)"
        Write-Output 'Broker writes    : NONE. The bridge has no order path.'
        Write-Output 'Nothing was changed.'
    }
    'Install' {
        if ($FireUrl -notmatch '^https://api\.anthropic\.com/v1/claude_code/routines/trig_[A-Za-z0-9]+/fire$') {
            throw 'Pass -FireUrl copied from claude.ai/code/routines (API trigger panel).'
        }
        if (-not (Test-Path -LiteralPath $liveDb -PathType Leaf)) { throw "Swift database not found: $liveDb" }
        if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
            throw "Scheduled task '$TaskName' already exists. Run -Action Uninstall first."
        }
        if (Test-Path -LiteralPath $configPath) { throw "Config already exists: $configPath" }
        New-Item -ItemType Directory -Force -Path (Join-Path $bridgeDir 'secrets') | Out-Null
        Protect-SwiftDirectory $bridgeDir
        $saltBytes = New-Object byte[] 24
        [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($saltBytes)
        $config = [ordered]@{
            live_db            = $liveDb
            outbox_db          = (Join-Path $bridgeDir 'outbox.sqlite3')
            state_dir          = $bridgeDir
            fire_url           = $FireUrl
            channel_hash_salt  = [Convert]::ToBase64String($saltBytes)
            entry_max_age_s    = 120
            followup_max_age_s = 3600
            hourly_fire_budget = 25
            http_timeout_s     = 15
        }
        $json = $config | ConvertTo-Json
        [System.IO.File]::WriteAllText($configPath, $json, (New-Object System.Text.UTF8Encoding($false)))
        Write-Output 'Paste the scheduled-task API token when prompted (input hidden; stored with DPAPI).'
        Invoke-Bridge @('enroll-token')
        $python = Get-SwiftPython $root
        $argLine = "-I -m swift_autotrader.cloud_bridge --config `"$configPath`" run --interval 2"
        $taskAction = New-ScheduledTaskAction -Execute $python -Argument $argLine -WorkingDirectory $bridgeDir
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
        $settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
            -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -StartWhenAvailable
        Register-ScheduledTask -TaskName $TaskName -Action $taskAction -Trigger $trigger -Settings $settings `
            -Description 'Swift review-only cloud bridge (no broker writes)' | Out-Null
        Invoke-Bridge @('run', '--once')
        Write-Output "Installed. Start now with: Start-ScheduledTask -TaskName $TaskName"
    }
    'Status' { Invoke-Bridge @('status') }
    'Pause' {
        [System.IO.File]::WriteAllText((Join-Path $bridgeDir 'bridge.paused'), (Get-Date).ToUniversalTime().ToString('o'))
        Write-Output 'Bridge paused: no further fires.'
    }
    'Resume' {
        Remove-Item -LiteralPath (Join-Path $bridgeDir 'bridge.paused') -ErrorAction SilentlyContinue
        Write-Output 'Bridge resumed.'
    }
    'Uninstall' {
        if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
            Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        }
        Write-Output "Task removed. Audit outbox kept at $bridgeDir. Delete that folder manually if unwanted."
    }
}
