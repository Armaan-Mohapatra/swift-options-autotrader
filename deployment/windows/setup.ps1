#requires -Version 5.1
<#
.SYNOPSIS
  Safe Swift PAPER deployment. Defaults to a completely non-mutating preview.
.EXAMPLE
  .\setup.ps1 -DryRun
.EXAMPLE
  .\setup.ps1 -Apply -ExpectedCommit <reviewed-40-character-SHA> -NoSchedule
#>
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
param(
    [string]$SourceRoot = '',
    [string]$InstallRoot = '',
    [ValidatePattern('^[0-9a-fA-F]{40}$')][string]$ExpectedCommit = '',
    [switch]$Apply,
    [switch]$DryRun,
    [switch]$NoSchedule,
    [string]$PackageZip = '',
    [ValidatePattern('^[0-9a-fA-F]{64}$')][string]$PackageSha256 = '',
    [switch]$NoDependencyDownload
)
. (Join-Path $PSScriptRoot 'common.ps1')
Assert-SwiftWindows
# PS 5.1 leaves $PSScriptRoot empty inside param defaults when CmdletBinding is used.
if ([string]::IsNullOrWhiteSpace($SourceRoot)) { $SourceRoot = Join-Path $PSScriptRoot '..\..' }
$source = [System.IO.Path]::GetFullPath($SourceRoot)
$packageMode = -not [string]::IsNullOrWhiteSpace($PackageZip)
$root = Resolve-SwiftInstallRoot -InstallRoot $InstallRoot
if (-not $packageMode -and
    (-not (Test-Path -LiteralPath (Join-Path $source 'uv.lock') -PathType Leaf) -or
    -not (Test-Path -LiteralPath (Join-Path $source 'pyproject.toml') -PathType Leaf))) {
    throw 'A reviewed checkout with pyproject.toml and uv.lock is required.'
}
if ($root.StartsWith($source.TrimEnd([char]'\') + '\', [StringComparison]::OrdinalIgnoreCase) -or
    $source.StartsWith($root.TrimEnd([char]'\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Installation and source checkout must be separate.'
}
$plan = [ordered]@{
    status = 'PREVIEW_NO_CHANGES'; mode = 'PAPER_ONLY'; source = $source; target = $root
    tasks = @('validate clean source commit', 'check Python 3.11 prerequisites',
        'isolate releases,data,secrets,logs,backups,state,config',
        'install pinned uv==0.10.0 then locked Python runtime dependencies',
        'initialize SQLite WAL, verify smoke and dashboard loopback',
        'register user-scoped restartable logon task unless -NoSchedule')
    package_archive = $(if ($packageMode) { $PackageZip } else { 'NONE' })
    live_broker_writes = $false
    telegram_authentication = 'MANUAL_LATER'
}
if (-not $Apply -or $DryRun -or $WhatIfPreference) {
    $plan | ConvertTo-Json -Depth 4
    return
}
if ([string]::IsNullOrWhiteSpace($ExpectedCommit)) {
    throw 'An explicitly reviewed full ExpectedCommit SHA is mandatory.'
}
if (-not $PSCmdlet.ShouldProcess($root, 'Install isolated Swift PAPER application')) { return }
if (-not (Get-Command py.exe -ErrorAction SilentlyContinue)) {
    throw 'Python Launcher not found. Install reviewed Python 3.11 x64 first.'
}
$pyVersion = & py.exe -3.11 -c 'import sys; print(sys.version_info[:2] == (3, 11))'
if ($LASTEXITCODE -ne 0 -or $pyVersion.Trim() -ne 'True') {
    throw 'Python 3.11 must be installed and selectable as py -3.11.'
}
$stage = ''
if ($packageMode) {
    # A SHA-pinned release ZIP may replace a Git checkout. Always verify
    # the owner-approved full commit, complete file manifest and SHA-256
    # BEFORE extracting, copying or running any package-supplied Python.
    if (-not $PackageSha256 -or -not (Test-Path -LiteralPath $PackageZip -PathType Leaf)) {
        throw 'Provide the approved release ZIP and its independent SHA-256 digest.'
    }
    $verifiedArchive = [System.IO.Path]::GetFullPath($PackageZip)
    $stage = Join-Path ([System.IO.Path]::GetTempPath()) ('SwiftPaperRelease-' +
               [Guid]::NewGuid().ToString('N'))
    # Verify the independently approved ZIP using Windows' own SHA-256 first.
    # Never execute a verifier from the launch directory or from an unchecked ZIP.
    $zipHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $verifiedArchive).Hash.ToLowerInvariant()
    if ($zipHash -ne $PackageSha256.ToLowerInvariant()) {
        throw 'Approved ZIP checksum mismatch; no package code was executed.'
    }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $reader = [System.IO.Compression.ZipFile]::OpenRead($verifiedArchive)
    $verifier = Join-Path ([System.IO.Path]::GetTempPath()) ('SwiftPaperVerifier-' +
                 [Guid]::NewGuid().ToString('N') + '.py')
    try {
        $verifierEntry = $reader.GetEntry('deployment/windows/extract_approved_package.py')
        if ($null -eq $verifierEntry -or $verifierEntry.Length -gt 2000000) {
            throw 'Approved ZIP has no bounded release verifier.'
        }
        $inputStream = $verifierEntry.Open()
        try {
            $outputStream = [System.IO.File]::Open($verifier, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
            try { $inputStream.CopyTo($outputStream) } finally { $outputStream.Dispose() }
        } finally { $inputStream.Dispose() }
    } finally { $reader.Dispose() }
    try {
        & py.exe -3.11 $verifier --archive $verifiedArchive --target $stage `
            --sha256 $PackageSha256.ToLowerInvariant() `
            --commit $ExpectedCommit.ToLowerInvariant()
        if ($LASTEXITCODE -ne 0) { throw 'Approved release verification failed.' }
    } finally {
        Remove-Item -LiteralPath $verifier -Force -ErrorAction SilentlyContinue
    }
    $source = $stage
    $actualCommit = $ExpectedCommit.ToLowerInvariant()
} else {
    if (-not (Get-Command git.exe -ErrorAction SilentlyContinue)) {
        throw 'Git is required to verify the source SHA and clean checkout.'
    }
    $actualCommit = (& git.exe -C $source rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0 -or $actualCommit -ne $ExpectedCommit.ToLowerInvariant()) {
        throw 'Source checkout does not match the reviewed commit.'
    }
    $dirty = (& git.exe -C $source status --porcelain)
    if ($LASTEXITCODE -ne 0 -or $dirty) {
        throw 'Source checkout has local changes; review them before installation.'
    }
}
if (Test-Path -LiteralPath $root) {
    $marker = Join-Path $root 'deployment.json'
    if (-not (Test-Path -LiteralPath $marker -PathType Leaf)) {
        if (@(Get-ChildItem -LiteralPath $root -Force).Count -gt 0) {
            throw 'InstallRoot contains unrelated or unmanaged files; refusing to overwrite.'
        }
    } else {
        Assert-SwiftManifest $root
    }
}
# No external data is read from any Phoenix/Kalshi environment or directory.
$folders = @('releases','data','logs','backups','state','secrets','config','tools','scripts')
New-Item -ItemType Directory -Path $root -Force | Out-Null
Protect-SwiftDirectory $root
foreach ($folder in $folders) {
    $p = Join-Path $root $folder
    New-Item -ItemType Directory -Path $p -Force | Out-Null
    Protect-SwiftDirectory $p
}
$release = Join-Path $root ('releases\' + $actualCommit)
if (Test-Path -LiteralPath $release) {
    # Successful repeated Apply of the same release is an idempotent no-op.
    $activeFile = Join-Path $root 'active-release.txt'
    $installedPython = Join-Path $release '.venv\Scripts\python.exe'
    if ((Test-Path -LiteralPath $activeFile -PathType Leaf) -and
        (Test-Path -LiteralPath $installedPython -PathType Leaf) -and
        (Get-Content -LiteralPath $activeFile -Raw -Encoding Ascii).Trim() -eq $actualCommit) {
        [ordered]@{ status = 'ALREADY_INSTALLED'; commit = $actualCommit;
                     root = $root; mode = 'PAPER_ONLY' } | ConvertTo-Json
        return
    }
    throw 'Incomplete or different release exists; stop for operator review.'
}
New-Item -ItemType Directory -Path $release -Force | Out-Null
# Copy source via strict allowlist; never copy .git, .env, sessions, logs or credentials.
foreach ($file in @('pyproject.toml','uv.lock','README.md','AGENTS.md')) {
    Copy-Item -LiteralPath (Join-Path $source $file) -Destination (Join-Path $release $file)
}
foreach ($file in @(Get-ChildItem -LiteralPath (Join-Path $source 'src') -Recurse -File)) {
    if ($file.Extension -notin @('.py','.sql')) { continue }
    if (($file.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw 'Symlink inside source tree; refusing to follow.'
    }
    $rel = $file.FullName.Substring($source.TrimEnd([char]'\').Length + 1)
    $target = Join-Path $release $rel
    New-Item -ItemType Directory -Path (Split-Path $target -Parent) -Force | Out-Null
    Copy-Item -LiteralPath $file.FullName -Destination $target
}
foreach ($file in @('common.ps1','setup.ps1','manage.ps1','service.ps1','secrets.ps1','healthcheck.ps1','activate-live.ps1','extract_approved_package.py')) {
    Copy-Item -LiteralPath (Join-Path $source ('deployment/windows/' + $file)) -Destination (Join-Path $root ('scripts\' + $file)) -Force
}
if (Test-Path -LiteralPath (Join-Path $source 'scripts\pin_account.py')) {
    Copy-Item -LiteralPath (Join-Path $source 'scripts\pin_account.py') -Destination (Join-Path $root 'scripts\pin_account.py') -Force
}
$manifest = Join-Path $root 'deployment.json'
if (-not (Test-Path -LiteralPath $manifest)) {
    $document = [ordered]@{
        product = 'swift-options-autotrader'; mode = 'PAPER_ONLY'
        live_enabled = $false; broker_writes_enabled = $false
        created_utc = [DateTime]::UtcNow.ToString('o')
        installation_guid = [Guid]::NewGuid().ToString('D')
    }
    [System.IO.File]::WriteAllText($manifest, ($document | ConvertTo-Json -Depth 3), ([System.Text.UTF8Encoding]::new($false)))
}
$settingsFile = Join-Path $root 'config\settings.json'
if (-not (Test-Path -LiteralPath $settingsFile)) {
    $settings = [ordered]@{
        mode = 'PAPER_ONLY'; live_enabled = $false
        telegram_enabled = $false; telegram_channel_id = $null; telegram_api_id = $null
        status_notifications_enabled = $false; status_chat_id = $null
        paper_dispatch_enabled = $false; paper_data_source = 'none'
        paper_max_contracts = 1; paper_max_premium_usd = '999'
        paper_max_daily_net_loss_usd = '750'
        # No exposure or open-trade cap: buying power alone bounds the trade count.
    }
    [System.IO.File]::WriteAllText($settingsFile, ($settings | ConvertTo-Json -Depth 3), ([System.Text.UTF8Encoding]::new($false)))
}
$uvvenv = Join-Path $root 'tools\uv-bootstrap'
if (-not (Test-Path -LiteralPath (Join-Path $uvvenv 'Scripts\uv.exe'))) {
    if ($NoDependencyDownload) { throw 'Pinned dependency bootstrap is unavailable in offline mode.' }
    & py.exe -3.11 -m venv $uvvenv
    if ($LASTEXITCODE -ne 0) { throw 'Could not create isolated uv bootstrap environment.' }
    $uvpy = Join-Path $uvvenv 'Scripts\python.exe'
    & $uvpy -m pip install --disable-pip-version-check --no-deps 'uv==0.10.0'
    if ($LASTEXITCODE -ne 0) { throw 'Pinned uv bootstrap failed.' }
}
$uvexe = Join-Path $uvvenv 'Scripts\uv.exe'
$uvVersion = (& $uvexe --version)
if ($LASTEXITCODE -ne 0 -or $uvVersion -notmatch '^uv 0\.10\.0(\s|$)') {
    throw 'Unexpected or unverified uv installer version.'
}
if ($NoDependencyDownload) { $env:UV_OFFLINE = '1' }
# uv writes normal notices (e.g. "Using CPython") to stderr. Under the global Stop preference,
# Windows PowerShell 5.1 turns that first line into a terminating error, so relax it here only.
$prevEap = $ErrorActionPreference
try {
    $ErrorActionPreference = 'Continue'
    & $uvexe sync --project $release --locked --no-dev --extra telegram --extra paper-market --no-editable --no-managed-python --python 3.11 2>&1 | ForEach-Object { Write-Host ([string]$_) }
    if ($LASTEXITCODE -ne 0) { throw 'Locked dependency installation failed.' }
} finally {
    $ErrorActionPreference = $prevEap
    Remove-Item Env:UV_OFFLINE -ErrorAction SilentlyContinue
}
$python = Join-Path $release '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw 'Package environment could not be verified.'
}
# An existing LIVE install keeps its LIVE mode through an upgrade (the same mode its
# launcher uses); fresh and paper installs stay PAPER_ONLY.
$installedMode = 'PAPER_ONLY'
$installedSettings = Join-Path $root 'config\settings.json'
if (Test-Path -LiteralPath $installedSettings -PathType Leaf) {
    $installedCfg = Get-Content -LiteralPath $installedSettings -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($installedCfg.mode -eq 'LIVE') { $installedMode = 'LIVE' }
}
Set-SwiftEnvironment -InstallRoot $root -Mode $installedMode
& $python -m swift_autotrader config-check
if ($LASTEXITCODE -ne 0) { throw 'Fail-closed configuration validation failed.' }
& $python -m swift_autotrader migrate
if ($LASTEXITCODE -ne 0) { throw 'Database initialization failed.' }
# Initialize isolated production journal with identical migrations
$liveDb = Join-Path $root 'data\live.sqlite3'
$origDb = $env:DB_PATH
try {
    $env:DB_PATH = $liveDb
    & $python -m swift_autotrader migrate
    if ($LASTEXITCODE -ne 0) { throw 'Production database initialization failed.' }
} finally {
    $env:DB_PATH = $origDb
}
& $python -m swift_autotrader smoke
if ($LASTEXITCODE -ne 0) { throw 'Application smoke test failed.' }
Remove-Item Env:TELEGRAM_API_HASH -ErrorAction SilentlyContinue
Remove-Item Env:TELEGRAM_STATUS_BOT_TOKEN -ErrorAction SilentlyContinue
$active = Join-Path $root 'active-release.txt'
$previous = Join-Path $root 'previous-release.txt'
if (Test-Path -LiteralPath $active) {
    Get-Content -LiteralPath $active -Raw -Encoding Ascii | Set-Content -LiteralPath $previous -Encoding Ascii
}
$actualCommit | Set-Content -LiteralPath $active -Encoding Ascii
if (-not $NoSchedule) {
    $taskName = 'SwiftAutoTrader-Paper-Local'
    $existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($null -ne $existing) { throw 'Scheduled task already exists. Review before replacing it.' }
    $runner = Join-Path $root 'scripts\service.ps1'
    $action = New-ScheduledTaskAction -Execute (Join-Path $PSHOME 'powershell.exe') `
        -Argument ('-NoProfile -NonInteractive -ExecutionPolicy RemoteSigned -File "' +
                   $runner + '" -InstallRoot "' + $root + '"')
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
}
if ($stage -and (Test-Path -LiteralPath $stage)) {
    Remove-Item -LiteralPath $stage -Recurse -Force
}
[ordered]@{
    status = 'INSTALLED_PAPER_ONLY'; commit = $actualCommit; install_root = $root
    scheduled = (-not $NoSchedule); live_broker_writes = $false
    telegram_enabled = $false; local_authorization = 'REQUIRED_BEFORE_TELEGRAM'
} | ConvertTo-Json -Depth 3
