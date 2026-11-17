#requires -Version 5.1
<# Start, stop, restart, status, health, backup, restore, rollback or uninstall
   ONLY the separately installed Swift PAPER task. Never touch other processes. #>
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Start','Stop','Restart','Status','Health','Backup','Restore','Rollback','Uninstall','Logs',
        'PauseEntries','ResumeEntries','EntriesStatus','Rehearse','InitLiveJournal')]
    [string]$Action,
    [string]$InstallRoot = '',
    [string]$BackupFile = '',
    [switch]$PurgeData,
    [switch]$IUnderstandDataLoss,
    [switch]$DryRun
)
. (Join-Path $PSScriptRoot 'common.ps1')
Assert-SwiftWindows
$root = Resolve-SwiftInstallRoot -InstallRoot $InstallRoot
Assert-SwiftManifest $root
$taskName = 'SwiftAutoTrader-Paper-Local'
$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
# Refuse to operate a colliding task owned by another application/installation.
if ($null -ne $task) {
    $expectedRunner = Join-Path $root 'scripts\service.ps1'
    $taskArguments = @($task.Actions | ForEach-Object { [string]$_.Arguments })
    if (@($taskArguments | Where-Object {
        $_.Contains('"' + $expectedRunner + '"') -and
        $_.Contains('"' + $root + '"')
    }).Count -ne 1) {
        throw 'Task name collision or task points to another installation. No changes made.'
    }
}
if ($DryRun -or $WhatIfPreference) {
    [ordered]@{ status = 'PREVIEW_NO_CHANGES'; action = $Action; root = $root;
        has_task = ($null -ne $task); live_broker_writes = $false } | ConvertTo-Json
    return
}
function Get-TaskState {
    if ($null -eq $task) { return 'NO_SCHEDULED_TASK' }
    return [string](Get-ScheduledTask -TaskName $taskName).State
}
function Stop-SwiftTask {
    if ($null -eq $task) { return }
    $marker = Join-Path $root 'state\stop.request'
    'STOP' | Set-Content -LiteralPath $marker -Encoding Ascii
    $deadline = [DateTime]::UtcNow.AddSeconds(35)
    do {
        if ((Get-TaskState) -ne 'Running') { break }
        Start-Sleep -Seconds 1
    } while ([DateTime]::UtcNow -lt $deadline)
    if ((Get-TaskState) -eq 'Running') {
        Stop-ScheduledTask -TaskName $taskName -ErrorAction Stop
    }
}
function Invoke-PythonRuntime {
    param([string[]]$Arguments)
    $exe = Get-SwiftPython $root
    & $exe -m swift_autotrader.windows_runtime @Arguments
    if ($LASTEXITCODE -ne 0) { throw 'Windows runtime command failed; see local logs.' }
}
function Invoke-SwiftCli {
    param([string[]]$Arguments)
    Set-SwiftEnvironment -InstallRoot $root
    & (Get-SwiftPython $root) -m swift_autotrader @Arguments
    if ($LASTEXITCODE -ne 0) { throw 'Swift command failed; see the JSON error above.' }
}
switch ($Action) {
    # Entry kill switch: takes effect on the next entry, no restart. Exits are never blocked.
    'PauseEntries' { Invoke-SwiftCli -Arguments @('entries-pause') }
    'EntriesStatus' { Invoke-SwiftCli -Arguments @('entries-status') }
    'ResumeEntries' {
        if ($PSCmdlet.ShouldProcess($root, 'Allow new entries again')) {
            Invoke-SwiftCli -Arguments @('entries-resume')
        }
    }
    # One-time: create the empty live journal at live_journal_path (never overwrites).
    'InitLiveJournal' { Invoke-SwiftCli -Arguments @('live-journal-init') }
    # Real MCP, writes forced off: logs what WOULD be sent. Never places or cancels.
    'Rehearse' {
        $env:LIVE_REHEARSAL = 'true'
        Invoke-SwiftCli -Arguments @('live-rehearsal', '--root', $root)
    }
    'Start' {
        if ($null -eq $task) { throw 'No scheduled task. Install with scheduling enabled.' }
        if ($PSCmdlet.ShouldProcess($taskName, 'Start isolated PAPER service')) {
            Remove-Item -LiteralPath (Join-Path $root 'state\stop.request') `
                -ErrorAction SilentlyContinue
            Start-ScheduledTask -TaskName $taskName
        }
    }
    'Stop' {
        if ($PSCmdlet.ShouldProcess($taskName, 'Stop isolated PAPER service')) {
            Stop-SwiftTask
        }
    }
    'Restart' {
        if ($null -eq $task) { throw 'No scheduled task registered.' }
        if ($PSCmdlet.ShouldProcess($taskName, 'Restart isolated PAPER service')) {
            Stop-SwiftTask
            Start-Sleep -Seconds 2
            Remove-Item -LiteralPath (Join-Path $root 'state\stop.request') `
                -ErrorAction SilentlyContinue
            Start-ScheduledTask -TaskName $taskName
        }
    }
    'Status' {
        $heartbeat = Join-Path $root 'state\health.json'
        $state = @{ task_state = (Get-TaskState); status = 'NOT_STARTED'; mode = 'PAPER_ONLY' }
        if (Test-Path -LiteralPath $heartbeat -PathType Leaf) {
            $state.status = 'HEARTBEAT_RECORDED'
            $state.heartbeat = Get-Content -LiteralPath $heartbeat -Raw -Encoding UTF8 | ConvertFrom-Json
        }
        $state | ConvertTo-Json -Depth 4
    }
    'Health' {
        Invoke-PythonRuntime -Arguments @('status', '--root', $root)
        try {
            $result = Invoke-WebRequest -Uri 'http://127.0.0.1:8765/api/status' `
                -UseBasicParsing -TimeoutSec 3 -ErrorAction Stop
            if ($result.StatusCode -ne 200) { throw 'Local dashboard unavailable.' }
            @{ local_dashboard = 'OK'; live_trading = $false } | ConvertTo-Json
        } catch {
            @{ local_dashboard = 'UNAVAILABLE'; live_trading = $false } | ConvertTo-Json
        }
    }
    'Backup' {
        if ($PSCmdlet.ShouldProcess($root, 'Write SQLite online backup')) {
            Invoke-PythonRuntime -Arguments @('backup', '--root', $root, '--keep', '7')
        }
    }
    'Restore' {
        if (-not $BackupFile) { throw 'Provide -BackupFile inside managed backups.' }
        if ((Get-TaskState) -eq 'Running') { throw 'Stop the task before restoration.' }
        if ($PSCmdlet.ShouldProcess($root, 'Restore offline SQLite from verified backup')) {
            Invoke-PythonRuntime -Arguments @('restore', '--root', $root,
                '--backup', $BackupFile, '--confirm-offline')
        }
    }
    'Rollback' {
        if ((Get-TaskState) -eq 'Running') { throw 'Stop the task before rollback.' }
        $previous = Join-Path $root 'previous-release.txt'
        if (-not (Test-Path -LiteralPath $previous -PathType Leaf)) {
            throw 'No previous managed release available.'
        }
        $sha = (Get-Content -LiteralPath $previous -Raw -Encoding UTF8).Trim()
        if ($sha -notmatch '^[0-9a-f]{40}$' -or -not (Test-Path -LiteralPath `
            (Join-Path $root "releases\$sha\.venv\Scripts\python.exe"))) {
            throw 'Prior managed release is invalid or unavailable.'
        }
        if ($PSCmdlet.ShouldProcess($root, 'Select previously installed code only')) {
            $active = Join-Path $root 'active-release.txt'
            $old = (Get-Content -LiteralPath $active -Raw -Encoding UTF8).Trim()
            $sha | Set-Content -LiteralPath $active -Encoding Ascii
            $old | Set-Content -LiteralPath $previous -Encoding Ascii
            'ROLLBACK_CODE_ONLY_DB_MIGRATIONS_NOT_DOWNGRADED'
        }
    }
    'Uninstall' {
        if ($PurgeData -and -not $IUnderstandDataLoss) {
            throw 'Data purge requires -IUnderstandDataLoss and explicit confirmation.'
        }
        if ($PSCmdlet.ShouldProcess($root, 'Unregister only Swift scheduled task')) {
            Stop-SwiftTask
            if ($null -ne $task) {
                Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
            }
            if ($PurgeData) {
                # Strict manifest+root validation above; no neighboring directories.
                Remove-Item -LiteralPath $root -Recurse -Force
                'PURGED_SWIFT_MANAGED_INSTALL_ONLY'
            } else {
                'TASK_REMOVED_DATA_LOGS_SECRETS_BACKUPS_AND_RELEASES_PRESERVED'
            }
        }
    }
    'Logs' {
        $file = Join-Path $root 'logs\supervisor.log'
        if (Test-Path -LiteralPath $file -PathType Leaf) {
            Get-Content -LiteralPath $file -Tail 40
        } else {
            'NO_SUPERVISOR_LOG'
        }
    }
}
