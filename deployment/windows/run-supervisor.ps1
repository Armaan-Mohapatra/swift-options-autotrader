#requires -Version 5.1
<#
.SYNOPSIS
    Runs the Swift Options AutoTrader cross-platform daemon supervisor on Windows.
.DESCRIPTION
    Ensures environment validation, resolves root directory, and invokes
    python -m swift_autotrader supervisor managing child worker lifecycles.
#>
[CmdletBinding()]
param(
    [string]$InstallRoot = '',
    [switch]$Once
)

. (Join-Path $PSScriptRoot 'common.ps1')
Assert-SwiftWindows
$root = Resolve-SwiftInstallRoot -InstallRoot $InstallRoot
Set-SwiftEnvironment -InstallRoot $root

try {
    $python = Get-SwiftPython $root
    $extraArgs = @()
    if ($Once) {
        $extraArgs += '--once'
    }
    & $python -m swift_autotrader supervisor --root $root @extraArgs
    exit $LASTEXITCODE
} finally {
    Remove-Item Env:TELEGRAM_API_HASH -ErrorAction SilentlyContinue
    Remove-Item Env:TELEGRAM_STATUS_BOT_TOKEN -ErrorAction SilentlyContinue
}
