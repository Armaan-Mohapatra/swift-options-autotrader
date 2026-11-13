#requires -Version 5.1
<# Manually enroll secrets on the approved Windows host. NEVER in CI. #>
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('TelegramApiHash','StatusBotToken','ImportTelegramSession')]
    [string]$Action,
    [string]$InstallRoot = '',
    [string]$SessionFile = '',
    [switch]$DryRun
)
. (Join-Path $PSScriptRoot 'common.ps1')
Assert-SwiftWindows
$root = Resolve-SwiftInstallRoot -InstallRoot $InstallRoot
Assert-SwiftManifest $root
if ($DryRun -or $WhatIfPreference) {
    [ordered]@{ status = 'PREVIEW_NO_CHANGES'; action = $Action;
        secrets_written = $false } | ConvertTo-Json
    return
}
$secrets = Join-Path $root 'secrets'
Protect-SwiftDirectory $secrets
if ($Action -eq 'ImportTelegramSession') {
    if (-not $SessionFile -or -not (Test-Path -LiteralPath $SessionFile -PathType Leaf)) {
        throw 'Choose an existing, already-authorized .session file.'
    }
    if ([System.IO.Path]::GetExtension($SessionFile) -ne '.session') {
        throw 'Only authorized Telegram .session files are accepted.'
    }
    $destination = Join-Path $secrets 'telegram.session'
    if (Test-Path -LiteralPath $destination) {
        throw 'Session already enrolled; review manually rather than overwriting.'
    }
    if ($PSCmdlet.ShouldProcess($destination, 'Copy user-authorized session')) {
        Copy-Item -LiteralPath $SessionFile -Destination $destination
        & icacls.exe $destination /inheritance:r /grant:r `
            "*$([System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value):F" `
            '*S-1-5-18:F' | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Session file ACL could not be restricted.' }
    }
} else {
    $filename = if ($Action -eq 'TelegramApiHash') {
        'telegram_api_hash.dpapi'
    } else { 'status_bot_token.dpapi' }
    $target = Join-Path $secrets $filename
    if ($PSCmdlet.ShouldProcess($target, 'Encrypt manually entered secret with user DPAPI')) {
        $value = Read-Host "Enter $Action (kept on this computer)" -AsSecureString
        try {
            ConvertFrom-SecureString -SecureString $value |
                Set-Content -LiteralPath $target -Encoding Ascii
        } finally {
            $value.Dispose()
        }
        & icacls.exe $target /inheritance:r /grant:r `
            "*$([System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value):F" `
            '*S-1-5-18:F' | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Encrypted secret ACL could not be restricted.' }
    }
}
'LOCAL_SECRET_ENROLLMENT_FINISHED_NO_BROKER_WRITES'
