#requires -Version 5.1
# Task Scheduler entrypoint. Running under the same logged-in Windows user is
# required for DPAPI secrets and local Telethon session file. PAPER ONLY.
[CmdletBinding()]
param([string]$InstallRoot = '')
. (Join-Path $PSScriptRoot 'common.ps1')
Assert-SwiftWindows
$root = Resolve-SwiftInstallRoot -InstallRoot $InstallRoot
Set-SwiftEnvironment -InstallRoot $root
try {
    $python = Get-SwiftPython $root
    & $python -m swift_autotrader.windows_runtime service --root $root
    exit $LASTEXITCODE
} finally {
    Remove-Item Env:TELEGRAM_API_HASH -ErrorAction SilentlyContinue
    Remove-Item Env:TELEGRAM_STATUS_BOT_TOKEN -ErrorAction SilentlyContinue
}
