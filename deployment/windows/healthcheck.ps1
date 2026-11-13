#requires -Version 5.1
# Only reads managed state and 127.0.0.1. No secret output or external calls.
[CmdletBinding()]
param([string]$InstallRoot = '')
. (Join-Path $PSScriptRoot 'common.ps1')
Assert-SwiftWindows
$root = Resolve-SwiftInstallRoot -InstallRoot $InstallRoot
Assert-SwiftManifest $root
$python = Get-SwiftPython $root
& $python -m swift_autotrader.windows_runtime status --root $root
exit $LASTEXITCODE
