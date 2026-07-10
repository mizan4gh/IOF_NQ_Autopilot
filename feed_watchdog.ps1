# feed_watchdog.ps1 — alert if the NQ data feed stalls during RTH.
# Reads the LAST RECORD TIMESTAMP inside NQU6.CME.scid (not file mtime, which
# NTFS updates lazily while Sierra holds the handle). If the newest tick is
# older than $StaleMin minutes during 09:25-16:05 ET on a weekday, pop a
# blocking alert + beep. Registered as scheduled task "IOF_FeedWatchdog"
# (every 10 min). Root cause: 2026-07-09 feed outage 07:13->17:32, zero live
# bars all session, autopilot blind — this watchdog exists so that never
# repeats silently.
#
# Exit codes: 0 = feed alive OR outside RTH window (nothing to do);
#             2 = STALL detected (alert emitted).
# Test hooks (do NOT set in production): -NowUtc injects the clock so the RTH
# gate and staleness math can be exercised deterministically; -NoAlert skips
# the beep + blocking dialog but still logs + prints + returns exit 2.
param(
    [string]$Scid = "C:\SierraChart\Data\NQU6.CME.scid",
    [int]$StaleMin = 10,
    [datetime]$NowUtc = [datetime]::UtcNow,
    [int]$AlertCooldownMin = 30,
    [switch]$NoAlert
)

$NowUtc = [datetime]::SpecifyKind($NowUtc, 'Utc')
$etz = [System.TimeZoneInfo]::FindSystemTimeZoneById("Eastern Standard Time")
$et  = [System.TimeZoneInfo]::ConvertTimeFromUtc($NowUtc, $etz)

# RTH gate — nothing to police outside the cash session.
if ($et.DayOfWeek -eq 'Saturday' -or $et.DayOfWeek -eq 'Sunday') { exit 0 }
$hhmm = $et.Hour * 100 + $et.Minute
if ($hhmm -lt 925 -or $hhmm -gt 1605) { exit 0 }

# ── Staleness detection ──────────────────────────────────────────────────────
$msg = $null
if (-not (Test-Path $Scid)) {
    $msg = "DATA FEED MISSING: $Scid not found. Check Sierra is running / connected NOW."
}
else {
    $f = [System.IO.File]::Open($Scid, 'Open', 'Read', 'ReadWrite')
    try {
        $recSize = 40; $hdr = 56
        $n = [math]::Floor(($f.Length - $hdr) / $recSize)
        if ($n -lt 1) { $msg = "DATA FEED EMPTY: $Scid has no records." }
        else {
            $br = New-Object System.IO.BinaryReader($f)
            $f.Seek($hdr + ($n - 1) * $recSize, 'Begin') | Out-Null
            $dtRaw = $br.ReadInt64()   # SCDateTime: microseconds since 1899-12-30, UTC
            # Decode straight off a UTC-kind epoch — no Get-Date "...Z"/ToUniversalTime
            # round-trip, which would mix the 1899 standard offset with 2026 EDT and
            # skew the age by an hour during daylight time (false alarms all summer).
            $scEpoch = [datetime]::new(1899, 12, 30, 0, 0, 0, [System.DateTimeKind]::Utc)
            $lastUtc = $scEpoch.AddSeconds($dtRaw / 1000000.0)
            $ageMin = ($NowUtc - $lastUtc).TotalMinutes
            if ($ageMin -gt $StaleMin) {
                $lastEt = [System.TimeZoneInfo]::ConvertTimeFromUtc([datetime]::SpecifyKind($lastUtc, 'Utc'), $etz)
                $msg = "DATA FEED STALLED: last NQU6 tick $([math]::Round($ageMin)) min ago ($($lastEt.ToString('HH:mm:ss')) ET). Check Sierra/Rithmic connection NOW."
            }
        }
    } finally { $f.Close() }
}

if (-not $msg) { exit 0 }

# ── Alert ────────────────────────────────────────────────────────────────────
$logPath = Join-Path $PSScriptRoot "feed_watchdog_alerts.log"
Add-Content -Path $logPath -Value "$($et.ToString('yyyy-MM-dd HH:mm:ss')) ET  $msg"
Write-Output $msg

if (-not $NoAlert) {
    # Cooldown: if we already alerted within the last $AlertCooldownMin, don't
    # stack another blocking modal — the log line above is the durable record.
    $recentAlert = $false
    if (Test-Path $logPath) {
        $lastLine = Get-Content $logPath -Tail 2 | Select-Object -First 1
        if ($lastLine -match '^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) ET') {
            $prevEt = [datetime]::ParseExact($matches[1], 'yyyy-MM-dd HH:mm:ss', $null)
            if (($et - $prevEt).TotalMinutes -lt $AlertCooldownMin) { $recentAlert = $true }
        }
    }
    for ($i = 0; $i -lt 5; $i++) { [console]::beep(1000, 400) }
    if (-not $recentAlert) {
        Add-Type -AssemblyName System.Windows.Forms
        [System.Windows.Forms.MessageBox]::Show($msg, "IOF Feed Watchdog", 'OK', 'Warning',
            'Button1', [System.Windows.Forms.MessageBoxOptions]::DefaultDesktopOnly) | Out-Null
    }
}
exit 2
