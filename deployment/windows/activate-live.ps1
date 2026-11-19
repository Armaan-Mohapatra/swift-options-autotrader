#requires -Version 5.1
<#
.SYNOPSIS
  Owner-operated final activation of unattended LIVE trading for Swift AutoTrader.

.DESCRIPTION
  Run it yourself, in your own logged-on Windows session, as the same user that enrolled
  the Robinhood OAuth token. It checks every gate before it changes anything, asks you to
  confirm locally, and stops at the first refusal. On any failure after the first change
  it restores the PAPER configuration, removes the receipt it wrote and restarts paper.

  It never places, changes or cancels an order. Entries stay PAUSED after activation
  until you type RESUME LIVE ENTRIES at the end.

  Preview every read-only gate and the exact changes, without changing anything:
    .\activate-live.ps1 -InstallRoot 'E:\SwiftAutoTrader\install-837aec03' -DryRun

  Activate (you will be asked to confirm):
    .\activate-live.ps1 -InstallRoot 'E:\SwiftAutoTrader\install-837aec03' -ValidityDays 7

  Stop new orders immediately at any time:
    manage.ps1 -InstallRoot '<root>' -Mode Live -Action PauseEntries
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$InstallRoot,
    [string]$CandidatePath = '',
    [ValidateRange(1, 7)][int]$ValidityDays = 7,
    [switch]$DryRun
)
. (Join-Path $PSScriptRoot 'common.ps1')
Assert-SwiftWindows
$root = Resolve-SwiftInstallRoot -InstallRoot $InstallRoot
Assert-SwiftManifest -InstallRoot $root -AllowedModes @('PAPER_ONLY')
$python = Get-SwiftPython $root
$manage = Join-Path $PSScriptRoot 'manage.ps1'
$service = Join-Path $PSScriptRoot 'service.ps1'
if (-not $CandidatePath) { $CandidatePath = Join-Path $root 'config\settings.live.candidate.json' }
$paperTask = 'SwiftAutoTrader-Paper-Local'
$liveTask = 'SwiftAutoTrader-Live-Local'
$backupDir = Join-Path $root ('backups\activation-' + (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ'))
$script:paperWasRunning = $false
$script:configChanged = $false
$script:receiptCreated = $false
$script:priorReceipt = $false
$script:liveStarted = $false
$receiptPath = Join-Path $root 'secrets\unattended-live-approval.json'

function Invoke-SwiftPy {
    param([string[]]$Arguments)
    $output = & $python -m swift_autotrader @Arguments
    $code = $LASTEXITCODE
    $text = ($output | Out-String).Trim()
    $json = $null
    if ($text) {
        try { $json = $text | ConvertFrom-Json } catch { $json = $null }
    }
    [pscustomobject]@{ Code = $code; Json = $json }
}

function Show-Gates {
    param($Report)
    foreach ($gate in @($Report.gates)) {
        $mark = if ($gate.passed) { 'PASS' } else { 'FAIL' }
        Write-Output ('  {0,-4} {1,-26} {2}' -f $mark, $gate.gate, $gate.reason)
    }
}

function Get-SwiftProcesses {
    # Count every Swift Python process, including children whose command lines omit the root.
    @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine.Contains('swift_autotrader') })
}

function Get-LiveDispatcherCount {
    $all = @(Get-SwiftProcesses)
    $ids = @($all | ForEach-Object { [int]$_.ProcessId })
    # A venv launcher and its child are one worker: count only processes whose parent is not Swift.
    @($all | Where-Object {
        $_.CommandLine.Contains('live-dispatch') -and -not ($ids -contains [int]$_.ParentProcessId)
    }).Count
}

function Wait-TaskNotRunning {
    param([string]$Name, [int]$Seconds)
    $deadline = (Get-Date).AddSeconds($Seconds)
    do {
        $task = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
        if ($null -eq $task -or $task.State -ne 'Running') { return $true }
        Start-Sleep -Seconds 1
    } while ((Get-Date) -lt $deadline)
    return $false
}

function Clear-LiveEnvironment {
    foreach ($name in @('SWIFT_INSTALL_ROOT','PAPER_ONLY','LIVE_ENABLED','ALLOW_BROKER_WRITES',
        'BROKER_BACKEND','DB_PATH','MAX_CONTRACTS_PER_SIGNAL','MAX_DAILY_LOSS_USD',
        'MAX_PREMIUM_PER_SIGNAL_USD','LIVE_JOURNAL_PATH','ENTRY_TIMEOUT_SECONDS','LOG_LEVEL',
        'TELEGRAM_ENABLED','TELEGRAM_QUEUE_CAPACITY','TELEGRAM_MAX_LIVE_AGE_SECONDS',
        'TELEGRAM_RECONNECT_ATTEMPTS','ENABLE_TELEGRAM_STATUS_NOTIFICATIONS','ROBINHOOD_MCP_ACCOUNT',
        'KILL_SWITCH_TELEGRAM_BOT_TOKEN','KILL_SWITCH_ENABLED','KILL_SWITCH_TELEGRAM_CHAT_ID',
        'TELEGRAM_API_HASH','SWIFT_TELEGRAM_CHANNEL_ID','TELEGRAM_API_ID','TELEGRAM_SESSION_PATH')) {
        Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue
    }
}

function Stop-LiveService {
    & $manage -Action Stop -Mode Live -InstallRoot $root -Confirm:$false | Out-Null
    [void](Wait-TaskNotRunning -Name $liveTask -Seconds 60)
}

function Restore-ActivationBackup {
    foreach ($name in @('settings.json','deployment.json')) {
        $saved = Join-Path $backupDir $name
        if (Test-Path -LiteralPath $saved -PathType Leaf) {
            Copy-Item -LiteralPath $saved -Destination (Join-Path $root $(if ($name -eq 'settings.json') { 'config\settings.json' } else { 'deployment.json' })) -Force
        }
    }
}

function Invoke-ActivationRollback {
    param([string]$Reason)
    Write-Output ('ROLLBACK: ' + $Reason)
    try {
        if ($script:liveStarted) { Stop-LiveService }
        Disable-ScheduledTask -TaskName $liveTask -ErrorAction SilentlyContinue | Out-Null
        if ($script:configChanged) { Restore-ActivationBackup }
        if ($script:receiptCreated -or $script:priorReceipt) {
            # Remove the receipt written by this run; put back the one it replaced, if any.
            Remove-Item -LiteralPath $receiptPath -Force -ErrorAction SilentlyContinue
            if ($script:priorReceipt) {
                Copy-Item -LiteralPath (Join-Path $backupDir 'secrets\unattended-live-approval.json') -Destination $receiptPath -Force
            }
        }
        if ($script:paperWasRunning) {
            & $manage -Action Start -InstallRoot $root -Confirm:$false | Out-Null
            Write-Output 'ROLLBACK: paper service restarted from the previous configuration.'
        }
        Write-Output 'ROLLBACK: complete. Live is disabled and no receipt remains.'
    } catch {
        Write-Output 'ROLLBACK_INCOMPLETE: check the live task, deployment.json and settings.json in the backup folder before retrying.'
    }
}

try {
    Write-Output '== 1. Read-only gates before any change'
    $pre = Invoke-SwiftPy -Arguments @('live-activation-check', '--root', $root, '--candidate', $CandidatePath, '--phase', 'pre')
    Show-Gates -Report $pre.Json
    if ($pre.Code -ne 0) { throw 'REFUSED: a pre-activation gate failed (FAIL lines above). Nothing was changed.' }

    Write-Output '== 2. Review the exact LIVE changes'
    $diff = Invoke-SwiftPy -Arguments @('live-activation-diff', '--root', $root, '--candidate', $CandidatePath)
    foreach ($line in @($diff.Json.changes)) { Write-Output ('  ' + $line) }
    Write-Output ('  Dedicated account ends in: ' + $diff.Json.account_ending)
    Write-Output ('  Receipt validity: ' + $ValidityDays + ' day(s), maximum 7.')
    Write-Output '  Limits: 1 long contract per eligible entry; premium at or below the candidate ceiling;'
    Write-Output '          daily realized-plus-unrealized loss breaker at the candidate limit.'
    Write-Output '  After activation: entries are PAUSED until you type RESUME LIVE ENTRIES.'
    if ($DryRun) {
        Write-Output 'DRY RUN: all gates and changes were shown. Nothing was changed.'
        return
    }

    Write-Output '== 3. Owner confirmation (typed locally)'
    $typed = (Read-Host 'Type the last four digits of the dedicated account shown above').Trim()
    if ($typed -ne [string]$diff.Json.account_ending) { throw 'REFUSED: account ending does not match. Nothing was changed.' }
    $phrase = Read-Host 'Type exactly ENABLE LIVE TRADING to continue (anything else cancels)'
    if ($phrase -cne 'ENABLE LIVE TRADING') { throw 'REFUSED: confirmation not given. Nothing was changed.' }

    Write-Output '== 4. Verified backups (configuration, manifest, tasks, secrets, control and journal databases)'
    New-Item -ItemType Directory -Path $backupDir | Out-Null
    foreach ($item in @('config\settings.json','deployment.json','config\settings.live.candidate.json')) {
        Copy-Item -LiteralPath (Join-Path $root $item) -Destination $backupDir -Force
    }
    foreach ($name in @($paperTask, $liveTask)) {
        Export-ScheduledTask -TaskName $name | Set-Content -LiteralPath (Join-Path $backupDir ($name + '.xml')) -Encoding UTF8
    }
    Copy-Item -LiteralPath (Join-Path $root 'secrets') -Destination (Join-Path $backupDir 'secrets') -Recurse -Force
    $script:priorReceipt = Test-Path -LiteralPath $receiptPath -PathType Leaf
    if (Test-Path -LiteralPath (Join-Path $root 'data\live.sqlite3')) {
        Copy-Item -LiteralPath (Join-Path $root 'data\live.sqlite3') -Destination $backupDir -Force
    }
    $journalSetting = (Get-Content -LiteralPath (Join-Path $root 'config\settings.live.candidate.json') -Raw -Encoding UTF8 | ConvertFrom-Json).live_journal_path
    if ($journalSetting -and (Test-Path -LiteralPath $journalSetting -PathType Leaf)) {
        Copy-Item -LiteralPath $journalSetting -Destination (Join-Path $backupDir 'live-journal.sqlite3') -Force
    }
    & $manage -Action Backup -InstallRoot $root -Confirm:$false | Out-Null
    $hashes = [ordered]@{}
    foreach ($item in @('settings.json','deployment.json')) {
        $source = Join-Path $root $(if ($item -eq 'settings.json') { 'config\settings.json' } else { 'deployment.json' })
        $copy = Join-Path $backupDir $item
        $hashes[$item] = (Get-FileHash -LiteralPath $copy -Algorithm SHA256).Hash
        if ((Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash -ne $hashes[$item]) { throw 'Backup verification failed.' }
    }
    $hashes | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $backupDir 'backup-hashes.json') -Encoding UTF8
    Write-Output ('  Backup written and verified: ' + $backupDir)

    Write-Output '== 5. Stop the paper task (no Telegram or journal data is deleted)'
    $paper = Get-ScheduledTask -TaskName $paperTask -ErrorAction SilentlyContinue
    if ($null -ne $paper -and $paper.State -eq 'Running') {
        $script:paperWasRunning = $true
        & $manage -Action Stop -InstallRoot $root -Confirm:$false | Out-Null
    }
    if (-not (Wait-TaskNotRunning -Name $paperTask -Seconds 90)) { throw 'Paper task did not stop within 90 seconds.' }
    $remaining = @(Get-SwiftProcesses).Count
    if ($remaining -ne 0) { throw ('REFUSED: ' + $remaining + ' Swift Python process(es) still running. Nothing else was changed.') }

    Write-Output '== 6. Broker account check (read-only get_accounts)'
    $account = Invoke-SwiftPy -Arguments @('live-account-check', '--root', $root)
    if ($account.Code -ne 0) { throw ('REFUSED: broker account check failed: ' + $account.Json.reason) }
    Write-Output ('  ' + $account.Json.reason + ' (ends ' + $account.Json.account_ending + ')')

    Write-Output '== 7. Apply LIVE configuration and pause entries'
    $script:configChanged = $true
    $applied = Invoke-SwiftPy -Arguments @('live-activation-apply', '--root', $root, '--candidate', $CandidatePath)
    if ($applied.Code -ne 0) { throw 'REFUSED: LIVE configuration could not be written.' }
    & $manage -Action PauseEntries -Mode Live -InstallRoot $root -Confirm:$false | Out-Null

    Write-Output '== 8. Owner receipt (written only after your confirmation above)'
    # A renewal replaces an expired receipt. The previous one is in the verified backup.
    Remove-Item -LiteralPath $receiptPath -Force -ErrorAction SilentlyContinue
    $receipt = Invoke-SwiftPy -Arguments @('live-activation-receipt', '--root', $root, '--candidate', $CandidatePath,
        '--account-last4', $typed, '--days', [string]$ValidityDays)
    if ($receipt.Code -ne 0) { throw 'REFUSED: owner receipt could not be written.' }
    $script:receiptCreated = $true
    Write-Output ('  Receipt expires: ' + $receipt.Json.expires_at)

    Write-Output '== 9. Post-write gates and the application configuration check'
    $post = Invoke-SwiftPy -Arguments @('live-activation-check', '--root', $root, '--candidate', $CandidatePath, '--phase', 'post')
    Show-Gates -Report $post.Json
    if ($post.Code -ne 0) { throw 'REFUSED: a post-write gate failed.' }
    try {
        Set-SwiftLiveEnvironment -InstallRoot $root
        $config = Invoke-SwiftPy -Arguments @('config-check')
    } finally {
        Clear-LiveEnvironment
    }
    if ($config.Code -ne 0 -or $config.Json.status -ne 'VALID') { throw 'REFUSED: application configuration validation failed.' }

    Write-Output '== 10. Enable and start the live task'
    & $service -Action RepairLiveTaskArgs -InstallRoot $root | Out-Null
    Enable-ScheduledTask -TaskName $liveTask | Out-Null
    Remove-Item -LiteralPath (Join-Path $root 'state\stop.request') -Force -ErrorAction SilentlyContinue
    $startedAt = (Get-Date).ToUniversalTime()
    Start-ScheduledTask -TaskName $liveTask
    $script:liveStarted = $true

    Write-Output '== 11. Verify the live worker starts and completes a cycle'
    $healthFile = Join-Path $root 'state\live-health.json'
    $verified = $false
    $deadline = (Get-Date).AddSeconds(240)
    do {
        Start-Sleep -Seconds 5
        if (Test-Path -LiteralPath $healthFile -PathType Leaf) {
            $health = Get-Content -LiteralPath $healthFile -Raw -Encoding UTF8 | ConvertFrom-Json
            $asOf = [DateTimeOffset]::Parse([string]$health.as_of_utc).UtcDateTime
            # Both operands are UTC DateTime values; DateTime has no UtcDateTime member.
            # A missing property can produce a null comparison and false startup verification.
            if ($asOf -ge $startedAt) {
                if ($health.status -eq 'FAILED') { throw 'REFUSED: the live worker reported FAILED at startup. See logs\supervisor.log.' }
                if ($health.mode -eq 'LIVE' -and $health.status -in @('RUNNING','DEGRADED') -and $null -ne $health.last_dispatch_status) {
                    $verified = $true
                    break
                }
            }
        }
    } while ((Get-Date) -lt $deadline)
    if (-not $verified) { throw 'The live worker did not complete a cycle within 240 seconds.' }
    $dispatchers = Get-LiveDispatcherCount
    if ($dispatchers -ne 1) { throw ('REFUSED: expected exactly one live dispatcher, found ' + $dispatchers + '.') }
    $task = Get-ScheduledTask -TaskName $liveTask
    if ($task.State -ne 'Running') { throw ('REFUSED: live task state is ' + $task.State + ', not Running.') }
    $entries = (& $manage -Action EntriesStatus -Mode Live -InstallRoot $root | Out-String)
    Write-Output ('  Live task: ' + $task.State + '; one dispatcher; ' + $entries.Trim())

    Write-Output '== 12. Resume entries (optional, explicit)'
    Write-Output 'The live service is running with NEW ENTRIES PAUSED. Exits and cancellations run normally.'
    $resume = Read-Host 'Type exactly RESUME LIVE ENTRIES to allow new orders now, or press Enter to keep them paused'
    if ($resume -ceq 'RESUME LIVE ENTRIES') {
        $final = Invoke-SwiftPy -Arguments @('live-activation-check', '--root', $root, '--candidate', $CandidatePath, '--phase', 'post')
        if ($final.Code -ne 0) { throw 'REFUSED: entries stay paused; a final gate failed.' }
        & $manage -Action ResumeEntries -Mode Live -InstallRoot $root -Confirm:$false | Out-Null
        $status = (& $manage -Action EntriesStatus -Mode Live -InstallRoot $root | Out-String)
        Write-Output ('  ' + $status.Trim())
    } else {
        Write-Output 'Entries remain PAUSED. Resume later with: manage.ps1 -Mode Live -Action ResumeEntries'
    }
    Write-Output 'ACTIVATION_COMPLETE'
} catch {
    Write-Output ('ACTIVATION_NOT_COMPLETED: ' + $_.Exception.Message)
    # Dry runs and failed preflight/owner confirmation must never mutate Task Scheduler.
    # Rollback only after this invocation actually began transitioning services/config.
    if ($script:paperWasRunning -or $script:configChanged -or $script:receiptCreated -or $script:liveStarted) {
        Invoke-ActivationRollback -Reason $_.Exception.Message
    } else {
        Write-Output 'NO_ACTIVATION_CHANGES: existing paper/live task states preserved.'
    }
    exit 2
}
