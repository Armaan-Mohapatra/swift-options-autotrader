#requires -Version 5.1
# Task Scheduler entrypoint and task management for SwiftAutoTrader.
# Running under the same logged-in Windows user is required for DPAPI secrets
# and local Telethon session file.
[CmdletBinding()]
param(
    [string]$InstallRoot = '',
    [ValidateSet('Run', 'RegisterLiveTask', 'UnregisterLiveTask')][string]$Action = 'Run',
    [ValidateSet('Paper', 'Live')][string]$Mode = 'Paper'
)
. (Join-Path $PSScriptRoot 'common.ps1')
Assert-SwiftWindows
$root = Resolve-SwiftInstallRoot -InstallRoot $InstallRoot

switch ($Action) {
    'RegisterLiveTask' {
        $taskName = 'SwiftAutoTrader-Live-Local'
        $existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        if ($null -ne $existing) { throw 'Scheduled live task already exists. Review before replacing it.' }
        $runner = Join-Path $root 'scripts\service.ps1'
        $action = New-ScheduledTaskAction -Execute (Join-Path $PSHOME 'powershell.exe') `
            -Argument ('-NoProfile -NonInteractive -ExecutionPolicy RemoteSigned -File "' +
                       $runner + '" -InstallRoot "' + $root + '" -Mode Live')
        $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
        $settings = New-ScheduledTaskSettingsSet -RestartCount 3 `
            -RestartInterval (New-TimeSpan -Minutes 2) -MultipleInstances IgnoreNew `
            -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries
        $principal = New-ScheduledTaskPrincipal -UserId $identity `
            -LogonType Interactive -RunLevel Limited
        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
            -Settings $settings -Principal $principal | Out-Null
        [ordered]@{ status = 'REGISTERED_LIVE_TASK'; task_name = $taskName; install_root = $root } | ConvertTo-Json
        return
    }
    'UnregisterLiveTask' {
        $taskName = 'SwiftAutoTrader-Live-Local'
        $existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        if ($null -ne $existing) {
            Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        }
        [ordered]@{ status = 'UNREGISTERED_LIVE_TASK'; task_name = $taskName } | ConvertTo-Json
        return
    }
    'Run' {
        if ($Mode -eq 'Paper') {
            $opposingTask = Get-ScheduledTask -TaskName 'SwiftAutoTrader-Live-Local' -ErrorAction SilentlyContinue
            if ($null -ne $opposingTask -and $opposingTask.State -eq 'Running') {
                throw 'Cannot run Paper service: SwiftAutoTrader-Live-Local is currently running.'
            }
            Set-SwiftEnvironment -InstallRoot $root -Mode 'PAPER_ONLY'
            try {
                $python = Get-SwiftPython $root
                & $python -m swift_autotrader.windows_runtime service --root $root
                exit $LASTEXITCODE
            } finally {
                Remove-Item Env:TELEGRAM_API_HASH -ErrorAction SilentlyContinue
                Remove-Item Env:TELEGRAM_STATUS_BOT_TOKEN -ErrorAction SilentlyContinue
            }
        } else {
            $opposingTask = Get-ScheduledTask -TaskName 'SwiftAutoTrader-Paper-Local' -ErrorAction SilentlyContinue
            if ($null -ne $opposingTask -and $opposingTask.State -eq 'Running') {
                throw 'Cannot run Live service: SwiftAutoTrader-Paper-Local is currently running.'
            }
            Set-SwiftEnvironment -InstallRoot $root -Mode 'LIVE'
            try {
                $python = Get-SwiftPython $root
                & $python -m swift_autotrader supervisor --root $root
                exit $LASTEXITCODE
            } finally {
                Remove-Item Env:TELEGRAM_API_HASH -ErrorAction SilentlyContinue
                Remove-Item Env:TELEGRAM_STATUS_BOT_TOKEN -ErrorAction SilentlyContinue
            }
        }
    }
}
