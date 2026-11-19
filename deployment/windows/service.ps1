#requires -Version 5.1
# Task Scheduler entrypoint and task management for SwiftAutoTrader.
# Running under the same logged-in Windows user is required for DPAPI secrets
# and local Telethon session file.
[CmdletBinding()]
param(
    [string]$InstallRoot = '',
    [ValidateSet('Run', 'RegisterLiveTask', 'UnregisterLiveTask', 'RepairLiveTaskArgs')][string]$Action = 'Run',
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
        # Not $action: variable names are case-insensitive and would overwrite the validated $Action parameter.
        $taskAction = New-ScheduledTaskAction -Execute (Join-Path $PSHOME 'powershell.exe') `
            -Argument ('-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy RemoteSigned -File "' +
                       $runner + '" -InstallRoot "' + $root + '" -Mode Live')
        $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
        $settings = New-ScheduledTaskSettingsSet -RestartCount 3 `
            -RestartInterval (New-TimeSpan -Minutes 2) -MultipleInstances IgnoreNew `
            -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries
        $principal = New-ScheduledTaskPrincipal -UserId $identity `
            -LogonType Interactive -RunLevel Limited
        Register-ScheduledTask -TaskName $taskName -Action $taskAction -Trigger $trigger `
            -Settings $settings -Principal $principal | Out-Null
        [ordered]@{ status = 'REGISTERED_LIVE_TASK'; task_name = $taskName; install_root = $root } | ConvertTo-Json
        return
    }
    'RepairLiveTaskArgs' {
        # Existing live task registered before -WindowStyle Hidden: a visible console
        # would stop live trading if closed. Only this installation's task is touched.
        $taskName = 'SwiftAutoTrader-Live-Local'
        $existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        if ($null -eq $existing) { throw 'No live task registered; register it first.' }
        if ($existing.State -eq 'Running') { throw 'Stop the live task before changing its definition.' }
        $runner = Join-Path $root 'scripts\service.ps1'
        $owned = @($existing.Actions | Where-Object {
            $text = [string]$_.Arguments
            $text.Contains('"' + $runner + '"') -and $text.Contains('"' + $root + '"') -and $text.Contains('-Mode Live')
        })
        if (@($existing.Actions).Count -ne 1 -or $owned.Count -ne 1) {
            throw 'Live task points to another installation or has unexpected actions. No changes made.'
        }
        $expected = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy RemoteSigned -File "' +
                    $runner + '" -InstallRoot "' + $root + '" -Mode Live'
        if ([string]$existing.Actions[0].Arguments -ceq $expected) {
            [ordered]@{ status = 'LIVE_TASK_ALREADY_CURRENT'; task_name = $taskName } | ConvertTo-Json
            return
        }
        $taskAction = New-ScheduledTaskAction -Execute (Join-Path $PSHOME 'powershell.exe') -Argument $expected
        Set-ScheduledTask -TaskName $taskName -Action $taskAction | Out-Null
        [ordered]@{ status = 'LIVE_TASK_ACTION_UPDATED'; task_name = $taskName } | ConvertTo-Json
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
                Remove-Item Env:KILL_SWITCH_TELEGRAM_BOT_TOKEN -ErrorAction SilentlyContinue
                Remove-Item Env:TELEGRAM_API_HASH -ErrorAction SilentlyContinue
                Remove-Item Env:TELEGRAM_STATUS_BOT_TOKEN -ErrorAction SilentlyContinue
            }
        }
    }
}
