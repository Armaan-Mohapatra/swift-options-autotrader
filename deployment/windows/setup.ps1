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
    [string]$SourceRoot = (Join-Path $PSScriptRoot '..\..'),
    [string]$InstallRoot = '',
    [ValidatePattern('^[0-9a-fA-F]{40}$')][string]$ExpectedCommit = '',
    [switch]$Apply,
    [switch]$DryRun,
    [switch]$NoSchedule,
    [switch]$NoDependencyDownload
)
. (Join-Path $PSScriptRoot 'common.ps1')
Assert-SwiftWindows
$source = [System.IO.Path]::GetFullPath($SourceRoot)
$root = Resolve-SwiftInstallRoot -InstallRoot $InstallRoot
if (-not (Test-Path -LiteralPath (Join-Path $source 'uv.lock') -PathType Leaf) -or
    -not (Test-Path -LiteralPath (Join-Path $source 'pyproject.toml') -PathType Leaf)) {
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
    throw 'Release already exists; use manage.ps1 rather than overwriting installed code.'
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
foreach ($file in @('common.ps1','setup.ps1','manage.ps1','service.ps1','secrets.ps1','healthcheck.ps1')) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot $file) -Destination (Join-Path $root ('scripts\' + $file)) -Force
}
$manifest = Join-Path $root 'deployment.json'
if (-not (Test-Path -LiteralPath $manifest)) {
    $document = [ordered]@{
        product = 'swift-options-autotrader'; mode = 'PAPER_ONLY'
        live_enabled = $false; broker_writes_enabled = $false
        created_utc = [DateTime]::UtcNow.ToString('o')
        installation_guid = [Guid]::NewGuid().ToString('D')
    }
    [System.IO.File]::WriteAllText($manifest, ($document | ConvertTo-Json -Depth 3), (New-Object System.Text.UTF8Encoding($false)))
}
$settingsFile = Join-Path $root 'config\settings.json'
if (-not (Test-Path -LiteralPath $settingsFile)) {
    $settings = [ordered]@{
        mode = 'PAPER_ONLY'; live_enabled = $false
        telegram_enabled = $false; telegram_channel_id = $null; telegram_api_id = $null
        status_notifications_enabled = $false; status_chat_id = $null
    }
    [System.IO.File]::WriteAllText($settingsFile, ($settings | ConvertTo-Json -Depth 3), (New-Object System.Text.UTF8Encoding($false)))
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
try {
    & $uvexe sync --project $release --locked --no-dev --extra telegram --no-editable --python 3.11
    if ($LASTEXITCODE -ne 0) { throw 'Locked dependency installation failed.' }
} finally {
    Remove-Item Env:UV_OFFLINE -ErrorAction SilentlyContinue
}
$python = Join-Path $release '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw 'Package environment could not be verified.'
}
Set-SwiftEnvironment -InstallRoot $root
& $python -m swift_autotrader config-check
if ($LASTEXITCODE -ne 0) { throw 'Fail-closed configuration validation failed.' }
& $python -m swift_autotrader migrate
if ($LASTEXITCODE -ne 0) { throw 'Database initialization failed.' }
& $python -m swift_autotrader smoke
if ($LASTEXITCODE -ne 0) { throw 'Application smoke test failed.' }
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
[ordered]@{
    status = 'INSTALLED_PAPER_ONLY'; commit = $actualCommit; install_root = $root
    scheduled = (-not $NoSchedule); live_broker_writes = $false
    telegram_enabled = $false; local_authorization = 'REQUIRED_BEFORE_TELEGRAM'
} | ConvertTo-Json -Depth 3
