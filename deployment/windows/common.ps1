# Phase 6 shared Windows helpers. Never run external processes on import.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Assert-SwiftWindows {
    if (-not [Environment]::OSVersion.Platform.ToString().StartsWith('Win')) {
        throw 'Windows PowerShell deployment is supported on Windows only.'
    }
}

function Resolve-SwiftInstallRoot {
    param([string]$InstallRoot = '')
    if ([string]::IsNullOrWhiteSpace($InstallRoot)) {
        if (-not $env:LOCALAPPDATA) { throw 'LOCALAPPDATA is not defined.' }
        $InstallRoot = Join-Path $env:LOCALAPPDATA 'SwiftAutoTraderPaper'
    }
    if (-not [System.IO.Path]::IsPathRooted($InstallRoot)) {
        throw 'InstallRoot must be an absolute local Windows path.'
    }
    $p = [System.IO.Path]::GetFullPath($InstallRoot).TrimEnd([char]'\', [char]'/')
    if ($p.StartsWith('\\') -or $p -match '(?i)(^|[\\/])(Phoenix|Kalshi)([\\/]|$)') {
        throw 'Network shares and Phoenix/Kalshi directories are forbidden.'
    }
    $driveRoot = [System.IO.Path]::GetPathRoot($p).TrimEnd([char]'\', [char]'/')
    if ($p -eq $driveRoot -or $p.Length -lt ($driveRoot.Length + 15)) {
        throw 'InstallRoot is too broad or near the drive root.'
    }
    if ($env:WINDIR -and $p.StartsWith($env:WINDIR.TrimEnd([char]'\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Cannot install under the Windows directory.'
    }
    if ($env:ProgramFiles -and $p.StartsWith($env:ProgramFiles.TrimEnd([char]'\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Cannot install under Program Files.'
    }
    # Block junctions and symlinked root/parents: no unrelated data redirection.
    $cursor = $p
    while ($cursor -and (Test-Path -LiteralPath $cursor)) {
        $item = Get-Item -LiteralPath $cursor -Force
        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw 'Installation path contains a reparse point.'
        }
        $parent = Split-Path -Path $cursor -Parent
        if ($parent -eq $cursor) { break }
        $cursor = $parent
    }
    return $p
}

function Assert-SwiftManifest {
    param(
        [string]$InstallRoot,
        [string[]]$AllowedModes = @('PAPER_ONLY', 'LIVE')
    )
    $marker = Join-Path $InstallRoot 'deployment.json'
    if (-not (Test-Path -LiteralPath $marker -PathType Leaf)) {
        throw 'No managed Swift installation at this path.'
    }
    $config = Get-Content -LiteralPath $marker -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($config.product -ne 'swift-options-autotrader') {
        throw 'Invalid or unsafe installation manifest.'
    }
    if ($AllowedModes -notcontains $config.mode) {
        throw "Installation mode '$($config.mode)' is not allowed (expected: $($AllowedModes -join ', '))."
    }
    if ($config.mode -eq 'PAPER_ONLY') {
        if ($config.live_enabled -ne $false -or $config.broker_writes_enabled -ne $false) {
            throw 'Invalid or unsafe installation manifest.'
        }
    } elseif ($config.mode -eq 'LIVE') {
        if ($config.live_enabled -ne $true -or $config.broker_writes_enabled -ne $true) {
            throw 'Invalid or unsafe live installation manifest.'
        }
    } else {
        throw 'Invalid or unsafe installation manifest.'
    }
}

function Get-SwiftActiveRelease {
    param([string]$InstallRoot)
    Assert-SwiftManifest $InstallRoot
    $file = Join-Path $InstallRoot 'active-release.txt'
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) {
        throw 'No activated application release.'
    }
    $sha = (Get-Content -LiteralPath $file -Raw -Encoding UTF8).Trim()
    if ($sha -notmatch '^[0-9a-f]{40}$') { throw 'Invalid active release fingerprint.' }
    return $sha
}

function Get-SwiftPython {
    param([string]$InstallRoot)
    $sha = Get-SwiftActiveRelease $InstallRoot
    $exe = Join-Path $InstallRoot "releases\$sha\.venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
        throw 'Active release virtual environment is missing.'
    }
    return $exe
}

function Protect-SwiftDirectory {
    param([string]$Path)
    Assert-SwiftWindows
    $sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    & icacls.exe $Path /inheritance:r /grant:r "*${sid}:(OI)(CI)F" '*S-1-5-18:(OI)(CI)F' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Failed to apply private Windows directory ACL.' }
}

function Set-SwiftEnvironment {
    param(
        [string]$InstallRoot,
        [ValidateSet('PAPER_ONLY', 'LIVE')][string]$Mode = 'PAPER_ONLY'
    )
    Assert-SwiftManifest -InstallRoot $InstallRoot -AllowedModes @($Mode)
    $configFile = Join-Path $InstallRoot 'config\settings.json'
    if (-not (Test-Path -LiteralPath $configFile -PathType Leaf)) {
        throw 'Managed non-secret settings file missing.'
    }
    $cfg = Get-Content -LiteralPath $configFile -Raw -Encoding UTF8 | ConvertFrom-Json
    # Older installs predate newer optional fields. Under StrictMode a missing property
    # throws before defaults apply, so add absent optional fields in memory only
    # (settings.json is never rewritten).
    foreach ($name in @(
        'live_journal_path', 'telegram_enabled', 'telegram_channel_id', 'telegram_api_id',
        'paper_dispatch_enabled', 'paper_data_source', 'paper_max_premium_usd',
        'paper_max_contracts', 'paper_max_exposure_usd', 'paper_max_open_signals',
        'paper_max_daily_net_loss_usd', 'status_notifications_enabled', 'status_chat_id'
    )) {
        if ($null -eq $cfg.PSObject.Properties[$name]) {
            $cfg | Add-Member -NotePropertyName $name -NotePropertyValue $null
        }
    }
    if ($Mode -eq 'PAPER_ONLY') {
        if ($cfg.mode -ne 'PAPER_ONLY' -or $cfg.live_enabled -ne $false) {
            throw 'Forbidden configuration: only paper mode is allowed.'
        }
        $env:SWIFT_INSTALL_ROOT = $InstallRoot
        # Drop inherited live-identifying variables (e.g. from another project) so
        # the paper service can never be pointed at a live journal or account.
        foreach ($v in 'LIVE_TRADING', 'LIVE_JOURNAL_PATH', 'LIVE_ACCOUNT_NUMBER',
                       'ROBINHOOD_MCP_ACCOUNT') {
            Remove-Item -LiteralPath "Env:$v" -ErrorAction SilentlyContinue
        }
        $env:PAPER_ONLY = 'true'
        $env:LIVE_ENABLED = 'false'
        $env:ALLOW_BROKER_WRITES = 'false'
        $env:BROKER_BACKEND = 'paper'
        $env:DB_PATH = Join-Path $InstallRoot 'data\swift.sqlite3'
        $env:MAX_CONTRACTS_PER_SIGNAL = '1'
        $env:MAX_PREMIUM_PER_SIGNAL_USD = '0'
        # Blank = no cap: trade count is bounded only by buying power.
        $env:PAPER_MAX_TOTAL_EXPOSURE_USD = ''
        $env:PAPER_MAX_OPEN_SIGNALS = ''
        $env:PAPER_MAX_DAILY_NET_LOSS_USD = '750'
        $env:ENTRY_TIMEOUT_SECONDS = '3600'
        $env:LOG_LEVEL = 'INFO'
        $env:TELEGRAM_ENABLED = 'false'
        $env:ENABLE_TELEGRAM_STATUS_NOTIFICATIONS = 'false'
        $env:PAPER_DISPATCH_ENABLED = 'false'
        $env:PAPER_MARKET_DATA_SOURCE = 'none'
        if ($cfg.telegram_enabled -eq $true) {
            if ($null -eq $cfg.telegram_channel_id -or $null -eq $cfg.telegram_api_id) {
                throw 'Telegram channel and API ID must be configured by the operator.'
            }
            $session = Join-Path $InstallRoot 'secrets\telegram.session'
            $hashFile = Join-Path $InstallRoot 'secrets\telegram_api_hash.dpapi'
            if (-not (Test-Path -LiteralPath $session -PathType Leaf) -or
                -not (Test-Path -LiteralPath $hashFile -PathType Leaf)) {
                throw 'Locally authorized Telegram session and DPAPI hash are required.'
            }
            $secret = Get-Content -LiteralPath $hashFile -Raw -Encoding UTF8 | ConvertTo-SecureString
            $bstr = [IntPtr]::Zero
            try {
                $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secret)
                $env:TELEGRAM_API_HASH = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
            } finally {
                if ($bstr -ne [IntPtr]::Zero) {
                    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
                }
            }
            $env:TELEGRAM_ENABLED = 'true'
            $env:SWIFT_TELEGRAM_CHANNEL_ID = [string]$cfg.telegram_channel_id
            $env:TELEGRAM_API_ID = [string]$cfg.telegram_api_id
            $env:TELEGRAM_SESSION_PATH = $session
        }
        if ($cfg.paper_dispatch_enabled -eq $true) {
            if ($cfg.telegram_enabled -ne $true -or
                $cfg.paper_data_source -ne 'robinhood_mcp_readonly') {
                throw 'Approved Telegram and official read-only MCP are both required.'
            }
            $tokenFile = Join-Path $InstallRoot 'secrets\oauth.dpapi'
            if (-not (Test-Path -LiteralPath $tokenFile -PathType Leaf)) {
                throw 'Manually enrolled Windows user OAuth tokens required.'
            }
            if ([string]$cfg.paper_max_premium_usd -notmatch '^[0-9]+(\.[0-9]{1,2})?$') {
                throw 'Approved paper premium budget must be a nonnegative exact dollar string.'
            }
            if ([decimal]$cfg.paper_max_premium_usd -le 0 -or
                [int]$cfg.paper_max_contracts -le 0) {
                throw 'Explicit positive PAPER-only premium and contract ceilings are required.'
            }
            # Exposure and open-trade caps are OPTIONAL (blank = unlimited, buying power only).
            $exposureCap = if ($null -eq $cfg.paper_max_exposure_usd) { '' } else { [string]$cfg.paper_max_exposure_usd }
            $openCap = if ($null -eq $cfg.paper_max_open_signals) { '' } else { [string]$cfg.paper_max_open_signals }
            if ($exposureCap -ne '' -and ($exposureCap -notmatch '^[0-9]+(\.[0-9]{1,2})?$' -or
                [decimal]$exposureCap -le 0)) {
                throw 'Optional exposure cap must be blank or a positive exact dollar string.'
            }
            if ($openCap -ne '' -and ($openCap -notmatch '^[0-9]+$' -or [int]$openCap -le 0)) {
                throw 'Optional open-trade cap must be blank or a positive integer.'
            }
            # Approved hard maxima; keep in sync with src/swift_autotrader/risk_limits.py.
            $dailyLoss = if ($null -eq $cfg.paper_max_daily_net_loss_usd) { '750' } else { [string]$cfg.paper_max_daily_net_loss_usd }
            if ($dailyLoss -notmatch '^[0-9]+(\.[0-9]{1,2})?$' -or [decimal]$dailyLoss -le 0 -or
                [decimal]$dailyLoss -gt 750) {
                throw 'Daily net loss ceiling must be above 0 and at most 750 USD.'
            }
            if ([decimal]$cfg.paper_max_premium_usd -gt 999 -or
                [int]$cfg.paper_max_contracts -gt 1) {
                throw 'PAPER-only risk ceilings exceed approved maxima (999 premium, 1 contract).'
            }
            $env:PAPER_MAX_DAILY_NET_LOSS_USD = $dailyLoss
            $env:PAPER_DISPATCH_ENABLED = 'true'
            $env:PAPER_MARKET_DATA_SOURCE = 'robinhood_mcp_readonly'
            $env:MAX_CONTRACTS_PER_SIGNAL = [string]$cfg.paper_max_contracts
            $env:MAX_PREMIUM_PER_SIGNAL_USD = [string]$cfg.paper_max_premium_usd
            $env:PAPER_MAX_TOTAL_EXPOSURE_USD = $exposureCap
            $env:PAPER_MAX_OPEN_SIGNALS = $openCap
        }
        if ($cfg.status_notifications_enabled -eq $true) {
            $tokenFile = Join-Path $InstallRoot 'secrets\status_bot_token.dpapi'
            if (-not (Test-Path -LiteralPath $tokenFile -PathType Leaf) -or
                $null -eq $cfg.status_chat_id) {
                throw 'Status bot token/chat target not configured.'
            }
            if ($cfg.status_chat_id -eq $cfg.telegram_channel_id) {
                throw 'Status notifications cannot be sent to the Swift source channel.'
            }
            $secret = Get-Content -LiteralPath $tokenFile -Raw -Encoding UTF8 | ConvertTo-SecureString
            $bstr = [IntPtr]::Zero
            try {
                $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secret)
                $env:TELEGRAM_STATUS_BOT_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
            } finally {
                if ($bstr -ne [IntPtr]::Zero) {
                    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
                }
            }
            $env:TELEGRAM_STATUS_CHAT_ID = [string]$cfg.status_chat_id
            $env:ENABLE_TELEGRAM_STATUS_NOTIFICATIONS = 'true'
        }
    } else {
        # LIVE PRODUCTION ENVIRONMENT
        if ($cfg.mode -ne 'LIVE' -or $cfg.live_enabled -ne $true -or $cfg.broker_writes_enabled -ne $true) {
            throw 'Forbidden configuration: settings not approved for production execution.'
        }
        $env:SWIFT_INSTALL_ROOT = $InstallRoot
        $env:PAPER_ONLY = 'false'
        $env:LIVE_ENABLED = 'true'
        $env:ALLOW_BROKER_WRITES = 'true'
        $env:BROKER_BACKEND = 'robinhood_mcp'
        $env:DB_PATH = Join-Path $InstallRoot 'data\live.sqlite3'
        $env:MAX_CONTRACTS_PER_SIGNAL = if ($cfg.max_contracts_per_signal) { [string]$cfg.max_contracts_per_signal } else { '1' }
        $env:MAX_DAILY_LOSS_USD = if ($cfg.max_daily_loss_usd) { [string]$cfg.max_daily_loss_usd } else { '750' }
        # Entries always use the exact alert limit; a slippage allowance is rejected by the app.
        Remove-Item Env:SLIPPAGE_TOLERANCE_PCT -ErrorAction SilentlyContinue
        $env:MAX_PREMIUM_PER_SIGNAL_USD = if ($cfg.max_premium_per_signal_usd) { [string]$cfg.max_premium_per_signal_usd } else { '0' }
        # No aggregate-exposure or open-trade cap: buying power is the only bound.
        Remove-Item Env:MAX_TOTAL_EXPOSURE_USD -ErrorAction SilentlyContinue
        if ([string]::IsNullOrWhiteSpace([string]$cfg.live_journal_path)) {
            throw 'Live execution requires live_journal_path (separate durable live journal).'
        }
        $env:LIVE_JOURNAL_PATH = [string]$cfg.live_journal_path
        $env:ENTRY_TIMEOUT_SECONDS = '3600'
        $env:LOG_LEVEL = if ($cfg.log_level) { [string]$cfg.log_level } else { 'INFO' }
        $env:TELEGRAM_ENABLED = 'true'
        $env:TELEGRAM_QUEUE_CAPACITY = '512'
        $env:TELEGRAM_MAX_LIVE_AGE_SECONDS = '120'
        $env:TELEGRAM_RECONNECT_ATTEMPTS = '5'
        $env:ENABLE_TELEGRAM_STATUS_NOTIFICATIONS = 'false'

        if ($null -eq $cfg.robinhood_mcp_account -or [string]::IsNullOrWhiteSpace($cfg.robinhood_mcp_account)) {
            throw 'Live execution requires a configured ROBINHOOD_MCP_ACCOUNT.'
        }
        $env:ROBINHOOD_MCP_ACCOUNT = [string]$cfg.robinhood_mcp_account

        if ($null -eq $cfg.telegram_channel_id -or $null -eq $cfg.telegram_api_id) {
            throw 'Telegram channel and API ID must be configured for live execution.'
        }
        $session = Join-Path $InstallRoot 'secrets\telegram.session'
        $hashFile = Join-Path $InstallRoot 'secrets\telegram_api_hash.dpapi'
        if (-not (Test-Path -LiteralPath $session -PathType Leaf) -or
            -not (Test-Path -LiteralPath $hashFile -PathType Leaf)) {
            throw 'Locally authorized Telegram session and DPAPI hash are required.'
        }
        $secret = Get-Content -LiteralPath $hashFile -Raw -Encoding UTF8 | ConvertTo-SecureString
        $bstr = [IntPtr]::Zero
        try {
            $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secret)
            $env:TELEGRAM_API_HASH = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
        } finally {
            if ($bstr -ne [IntPtr]::Zero) {
                [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
            }
        }
        $env:SWIFT_TELEGRAM_CHANNEL_ID = [string]$cfg.telegram_channel_id
        $env:TELEGRAM_API_ID = [string]$cfg.telegram_api_id
        $env:TELEGRAM_SESSION_PATH = $session

        $tokenFile = Join-Path $InstallRoot 'secrets\oauth.dpapi'
        if (-not (Test-Path -LiteralPath $tokenFile -PathType Leaf)) {
            throw 'Live execution requires enrolled Windows user OAuth tokens at secrets\oauth.dpapi.'
        }
        $pinnedAccountFile = Join-Path $InstallRoot 'secrets\agentic_account.dpapi'
        if (-not (Test-Path -LiteralPath $pinnedAccountFile -PathType Leaf)) {
            throw 'Live execution requires confirmed pinned account at secrets\agentic_account.dpapi.'
        }
    }
}

function Set-SwiftLiveEnvironment {
    param([string]$InstallRoot)
    Set-SwiftEnvironment -InstallRoot $InstallRoot -Mode 'LIVE'
}
