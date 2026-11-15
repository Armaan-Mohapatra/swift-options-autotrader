# Swift AutoTrader — focused safe-mode recovery

## Evidence and scope
- Original receiver launcher rejected every enabled Swift task other than itself. The unchanged paper launcher requires the existing receiver and a fresh heartbeat. Together, the exact archived sources demonstrate a mutual startup dependency conflict, conditional on the laptop still using those bytes. Other causes of exit code 2 remain possible.
- Fresh independent Linux regression run: 188 passed, one warning in 2.05 seconds; network, external execution, SQLite guards passed. Earlier user-pasted Windows results: 188 passed, plus 7/7 synthetic rehearsal checks. These results apply to the original baseline, not the new PowerShell patch.
- Verified 83 baseline manifest entries and 66 paper manifest entries from the original archives.
- New script defaults to read-only diagnostics. Explicit `-RepairAndStart` patches only the exact recognized receive-only launcher, retains a backup, leaves existing task registrations in place, starts receiver, checks heartbeat and process identity, then starts the existing paper pilot. It never clears stop markers or removes locks. It will refuse unrecognized source, tasks, release markers or residual detected workers. It does not enable live trading.
- No native PowerShell runtime was available in this review environment. Native runtime validation and actual task repair remain pending; no laptop files or tasks have been modified by ChatGPT.

## Static artifact verification
- original receiver file pin: PASS
- original completion source manifests: PASS
- all 66 paper manifest entries: PASS
- paper manifest and verifier pins: PASS
- all 83 baseline source manifest entries: PASS
- baseline review gate unchanged: PASS
- embedded replacement matches derived exact patch: PASS
- original runtime safety flags retained: PASS
- paper-only sibling exception requires manifest check and exact action: PASS
- no activation, broker-write or task re-registration calls in recovery: PASS
- default diagnosis branch precedes replacement and task start: PASS
- native package syntax check precedes launcher replacement: PASS

Recovery script SHA-256: `99b0b86b7f29f68d124dfefbc574ba145613ea2be6f16866f221ffd6e697a6ff`
Original launcher SHA-256: `0e31fd7ca4ef3073b72085f8aaac702aa39dee3df78f3d7dca2b8440bb7fb3b1`
Replacement launcher SHA-256: `a5573bd259b58c63c1f3ed7836eeee9271cd9ce4b0832f1886b47caff0bb248c`

## Not adopted from Gemini
The new supervisor generated only heartbeats, attempted reconciliation against an empty list, and used a parallel journal. The new policy parser used in-memory dedupe, loose contract matching and a different age rule. Certification validated structural fields rather than authenticated provenance; release repair deleted lockfiles without identity checks and repackaged a mutable installation. These are not upgrades to the verified baseline.

## Remaining requirements
Restore receiver and paper service; verify market-session fresh alerts, current quotes and forward paper fills; validate actual provider response contracts, position valuation and ownership, unknown-order recovery and production lifecycle with evidence; independently review exact production release and applicable owner/provider authorization. Real-money activation is deliberately not included.

## Launcher change (unified diff)
```diff
--- original/Run-ReceiveOnly.ps1
+++ patched/Run-ReceiveOnly.ps1
@@ -6,23 +6,53 @@
 $release='0c090e034ba7a761a1eb1a0cd08e06c91ddf8098'
 $python=Join-Path $root "releases\$release\.venv\Scripts\python.exe"
 if(Test-Path -LiteralPath (Join-Path $PSScriptRoot 'monitor.disabled')){exit 0}
+$phase='runtime_verification'
 try {
     $v=& $python -I -B (Join-Path $PSScriptRoot 'verify.py') --runtime-only
     if($LASTEXITCODE -ne 0){throw 'Local safety checks blocked monitor.'}
     $check=$v | ConvertFrom-Json
     if($check.lock -ne 'AVAILABLE_AT_CHECK'){throw 'Service lock unavailable.'}
+    $phase='installed_manifest'
     . (Join-Path $root 'scripts\common.ps1')
     Assert-SwiftManifest $root
     if((Get-SwiftActiveRelease $root) -ne $release){throw 'Active release mismatch.'}
-    # Reject known conflicting scheduled service even if it is currently idle.
-    $other=@(Get-ScheduledTask | Where-Object {$_.TaskName -match 'SwiftAutoTrader|Swift.*Companion' -and $_.TaskName -ne 'SwiftAutoTrader-ReceiveOnly-Local' -and $_.State -ne 'Disabled'})
-    if($other.Count -gt 0){throw 'Another Swift scheduled task is enabled.'}
+    # Permit ONLY the independently verified isolated paper pilot. Every other enabled
+    # Swift scheduled service remains a conflict, including differently named companions.
+    $phase='scheduled_task_coexistence'
+    $other=@(Get-ScheduledTask | Where-Object {$_.TaskName -match 'SwiftAutoTrader|Swift.*Companion' -and ($_.TaskName -ne 'SwiftAutoTrader-ReceiveOnly-Local' -or $_.TaskPath -ne '\') -and $_.State -ne 'Disabled'})
+    $paper=@($other | Where-Object {$_.TaskName -eq 'SwiftAutoTrader-PaperPilot-Local' -and $_.TaskPath -eq '\'})
+    if($other.Count -ne $paper.Count -or $paper.Count -gt 1){throw 'Another Swift scheduled task is enabled.'}
+    if($paper.Count -eq 1){
+        $phase='paper_task_identity'
+        $paperDir=Join-Path $env:LOCALAPPDATA 'SwiftPaperPilot-20261114'
+        $paperLaunch=Join-Path $paperDir 'Run-PaperPilot.ps1'
+        $pa=@($paper[0].Actions)
+        $expectedArgs='-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "'+$paperLaunch+'"'
+        if($pa.Count -ne 1 -or $pa[0].Execute -ine (Join-Path $PSHOME 'powershell.exe') -or $pa[0].Arguments -ine $expectedArgs){throw 'Paper task action differs.'}
+        $identity=[System.Security.Principal.WindowsIdentity]::GetCurrent()
+        $ownerId=[string]$paper[0].Principal.UserId
+        if($ownerId -match '^S-1-'){$ownerSid=$ownerId}else{$ownerSid=([System.Security.Principal.NTAccount]::new($ownerId)).Translate([System.Security.Principal.SecurityIdentifier]).Value}
+        if($ownerSid -ne $identity.User.Value -or [string]$paper[0].Principal.LogonType -notin @('Interactive','InteractiveToken') -or [string]$paper[0].Settings.MultipleInstances -ne 'IgnoreNew'){throw 'Paper task ownership or duplicate policy differs.'}
+        $phase='paper_package_integrity'
+        $paperManifest=Join-Path $paperDir 'PACKAGE_SHA256.json'
+        $paperVerifier=Join-Path $paperDir 'verify_package.py'
+        foreach($p in @($paperDir,$paperManifest,$paperVerifier,$paperLaunch)){
+            if((Get-Item -LiteralPath $p -Force).Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'Redirected paper package path.'}
+        }
+        if((Get-FileHash -LiteralPath $paperManifest -Algorithm SHA256).Hash -ine '2cb07bd0431796e79835512f9c2bc701cb070281a4fb485384f5b2a21110557b' -or (Get-FileHash -LiteralPath $paperVerifier -Algorithm SHA256).Hash -ine '0bd01815d66503c93fe0d2818ca0b8319bf52773a49d0dd6fbf5d646606b75c3'){throw 'Paper package identity differs.'}
+        $pcheck=& $python -I -B $paperVerifier
+        if($LASTEXITCODE -ne 0){throw 'Paper package verification failed.'}
+        $presult=$pcheck | ConvertFrom-Json
+        if($presult.status -ne 'PACKAGE_VERIFIED' -or $presult.files -ne 66){throw 'Paper package evidence incomplete.'}
+    }
+    $phase='receiver_environment'
     Set-SwiftEnvironment -InstallRoot $root
     $env:PAPER_ONLY='true'; $env:LIVE_ENABLED='false'; $env:ALLOW_BROKER_WRITES='false'; $env:BROKER_BACKEND='paper'
     $env:PAPER_DISPATCH_ENABLED='false'; $env:PAPER_MARKET_DATA_SOURCE=''
     $env:MAX_PREMIUM_PER_SIGNAL_USD='0'; $env:PAPER_MAX_TOTAL_EXPOSURE_USD='0'
     Remove-Item Env:PYTHONPATH,Env:PYTHONHOME -ErrorAction SilentlyContinue
     if($env:TELEGRAM_ENABLED -ne 'true' -or $env:DB_PATH -ne (Join-Path $root 'data\swift.sqlite3')){throw 'Unexpected receiver configuration.'}
+    $phase='notification_credentials'
     $enc=Get-Content -LiteralPath (Join-Path $PSScriptRoot 'notification.dpapi.json') -Raw | ConvertFrom-Json
     $secure=ConvertTo-SecureString -String $enc.token
     $env:TELEGRAM_STATUS_BOT_TOKEN=[System.Net.NetworkCredential]::new('',$secure).Password
@@ -32,13 +62,17 @@
     if(Test-Path -LiteralPath (Join-Path $PSScriptRoot 'monitor.disabled')){exit 0}
     # Recheck operator stop markers; existing supervisor never gets launched against a known stop request.
     foreach($marker in @('stop.request','SWIFT_REVIEWED_STOP')){if(Test-Path -LiteralPath (Join-Path $root "state\$marker")){throw 'Stop marker present.'}}
+    $phase='receiver_worker'
     Push-Location $HOME
     try { & $python -I -B -m swift_autotrader.windows_runtime service --root $root; $rc=$LASTEXITCODE }
     finally { Pop-Location }
     exit $rc
 } catch {
     # Do not print raw exception messages, which may include credentials.
-    Write-Host 'RECEIVE_ONLY_MONITOR_BLOCKED; inspect local state and lock.'
+    Write-Host ('RECEIVE_ONLY_MONITOR_BLOCKED; phase='+$phase)
+    try{
+        [ordered]@{status='BLOCKED';phase=$phase;error_type=$_.Exception.GetType().Name;as_of_utc=[DateTimeOffset]::UtcNow.ToString('o');live_certified=$false} | ConvertTo-Json -Compress | Set-Content -LiteralPath (Join-Path $PSScriptRoot 'STARTUP-ERROR.json') -Encoding UTF8
+    }catch{}
     exit 2
 } finally {
     Remove-Item Env:TELEGRAM_API_HASH,Env:TELEGRAM_STATUS_BOT_TOKEN,Env:TELEGRAM_STATUS_CHAT_ID -ErrorAction SilentlyContinue
```
