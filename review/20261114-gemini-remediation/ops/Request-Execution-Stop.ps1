# Review-only operational draft. Does not cancel broker orders or stop the receiver.
[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$InstallRoot)
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
$root=(Resolve-Path -LiteralPath $InstallRoot).Path
$state=Join-Path $root 'state'
$managed=Get-Content -LiteralPath (Join-Path $root 'deployment.json') -Raw | ConvertFrom-Json
if ($managed.product -ne 'swift-options-autotrader') { throw 'Not a managed Swift root' }
foreach ($p in @($root,$state)) {
    $item=Get-Item -LiteralPath $p -Force
    if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Unsafe root/state' }
}
$marker=Join-Path $state 'SWIFT_REVIEWED_STOP'
if (-not (Test-Path -LiteralPath $marker)) {
    $stream=[IO.File]::Open($marker,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::Read)
    try {
        $bytes=[Text.Encoding]::UTF8.GetBytes('EXECUTION_STOP_REQUESTED '+[DateTime]::UtcNow.ToString('o'))
        $stream.Write($bytes,0,$bytes.Length); $stream.Flush($true)
    } finally { $stream.Dispose() }
}
$item=Get-Item -LiteralPath $marker -Force
if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Unsafe marker' }
[ordered]@{ status='STOP_REQUESTED_NOT_VERIFIED'; marker_present=$true; process_stopped_verified=$false; broker_orders_cancelled=$false; receiver_changed=$false; note='Do not clear marker. Verify execution task/process and all working orders/positions independently.' } | ConvertTo-Json
