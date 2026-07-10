# Drives feed_watchdog.ps1 through controlled fire / no-fire conditions using
# synthetic scids with known last-tick timestamps and an injected clock.
$ErrorActionPreference = 'Stop'
$wd  = "C:\Users\17034\MyFolder\IOF_NQ_Production_Final\feed_watchdog.ps1"
$dir = $PSScriptRoot
$logPath = "C:\Users\17034\MyFolder\IOF_NQ_Production_Final\feed_watchdog_alerts.log"

function New-TestScid {
    param([string]$Path, [datetime]$LastTickUtc)
    $epoch = [datetime]::new(1899, 12, 30, 0, 0, 0, [System.DateTimeKind]::Utc)
    $lt    = [datetime]::SpecifyKind($LastTickUtc, 'Utc')
    $dtRaw = [int64](($lt - $epoch).TotalSeconds * 1000000.0)
    $fs = [System.IO.File]::Open($Path, 'Create', 'Write')
    $bw = New-Object System.IO.BinaryWriter($fs)
    $bw.Write((New-Object byte[] 56))                      # header (reader skips it)
    $bw.Write([int64]$dtRaw)                               # SCDateTime
    $bw.Write([float]100); $bw.Write([float]101); $bw.Write([float]99); $bw.Write([float]100.5)
    $bw.Write([uint32]1); $bw.Write([uint32]10); $bw.Write([uint32]5); $bw.Write([uint32]5)
    $bw.Flush(); $bw.Close(); $fs.Close()
}

# Reference clocks (UTC wall-clock). July = EDT, so ET = UTC-4.
$rth     = [datetime]::new(2026, 7, 9, 14, 0, 0, [System.DateTimeKind]::Utc)  # Thu 10:00 ET
$offhrs  = [datetime]::new(2026, 7, 9,  7, 0, 0, [System.DateTimeKind]::Utc)  # Thu 03:00 ET
$weekend = [datetime]::new(2026, 7, 11, 14, 0, 0, [System.DateTimeKind]::Utc) # Sat 10:00 ET

$cases = @(
    @{ name = 'fresh_rth (2m old, in window)';      now = $rth;     ageMin = 2;  missing = $false; expect = 0 }
    @{ name = 'fresh_boundary (9m old, floor=10)';  now = $rth;     ageMin = 9;  missing = $false; expect = 0 }
    @{ name = 'stall_boundary (11m old, floor=10)'; now = $rth;     ageMin = 11; missing = $false; expect = 2 }
    @{ name = 'stall_rth (30m old, in window)';     now = $rth;     ageMin = 30; missing = $false; expect = 2 }
    @{ name = 'stall_offhours (30m old, 03:00 ET)'; now = $offhrs;  ageMin = 30; missing = $false; expect = 0 }
    @{ name = 'stall_weekend (30m old, Saturday)';  now = $weekend; ageMin = 30; missing = $false; expect = 0 }
    @{ name = 'missing_scid (in window)';           now = $rth;     ageMin = 0;  missing = $true;  expect = 2 }
)

$pass = 0; $fail = 0
foreach ($c in $cases) {
    if ($c.missing) {
        $scid = Join-Path $dir "does_not_exist_$([guid]::NewGuid()).scid"
    } else {
        $scid = Join-Path $dir "wd_test.scid"
        New-TestScid -Path $scid -LastTickUtc $c.now.AddMinutes(-$c.ageMin)
    }

    $logBefore = if (Test-Path $logPath) { (Get-Content $logPath | Measure-Object -Line).Lines } else { 0 }
    $nowStr = $c.now.ToString('yyyy-MM-dd HH:mm:ss')

    $out = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $wd `
        -Scid $scid -StaleMin 10 -NowUtc $nowStr -NoAlert 2>&1
    $code = $LASTEXITCODE

    $logAfter = if (Test-Path $logPath) { (Get-Content $logPath | Measure-Object -Line).Lines } else { 0 }
    $logDelta = $logAfter - $logBefore

    $expectLog = if ($c.expect -eq 2) { 1 } else { 0 }
    $ok = ($code -eq $c.expect) -and ($logDelta -eq $expectLog)
    if ($ok) { $pass++; $tag = 'PASS' } else { $fail++; $tag = 'FAIL' }

    $stdout = ($out | Where-Object { $_ -is [string] -and $_ -match '\S' } | Select-Object -First 1)
    "{0}  {1,-34} exit={2} (want {3})  log+={4}  {5}" -f $tag, $c.name, $code, $c.expect, $logDelta, $stdout
    if (-not $c.missing) { Remove-Item $scid -ErrorAction SilentlyContinue }
}

""
"RESULT: $pass passed, $fail failed"
if ($fail -gt 0) { exit 1 } else { exit 0 }
