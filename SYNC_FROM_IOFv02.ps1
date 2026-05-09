<#
.SYNOPSIS
  Copy IOF NQ production sources from IOFv02 into this folder (one-way refresh).

.DESCRIPTION
  Defaults: main study .cpp plus optional header mirrors (iof_unified, iof_v1_hooks.h)
  for local reference. Sierra remote build only needs the single .cpp when logic is inlined.

.PARAMETER CppOnly
  Copy only IOF_NQ_Autopilot.cpp (fastest; smallest surface for ACS_Source uploads).

.PARAMETER CopySierraDocs
  Also copy SIERRA_REPLAY_CHECKLIST.txt and BUILD.txt from IOFv02.

.PARAMETER NoVersionStamp
  Do not rewrite VERSION.txt with last-sync metadata.

.EXAMPLE
  .\SYNC_FROM_IOFv02.ps1

.EXAMPLE
  .\SYNC_FROM_IOFv02.ps1 -CppOnly

.EXAMPLE
  .\SYNC_FROM_IOFv02.ps1 -CopySierraDocs -WhatIf
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [switch] $CppOnly,
    [switch] $CopySierraDocs,
    [switch] $NoVersionStamp
)

Set-StrictMode -Version 3.0
$ErrorActionPreference = "Stop"

$here = $PSScriptRoot
$root = Split-Path -Parent $here
$src = Join-Path $root "IOFv02"
$mainCpp = "IOF_NQ_Autopilot.cpp"
$srcCpp = Join-Path $src $mainCpp

if (-not (Test-Path -LiteralPath $srcCpp)) {
    Write-Error "Source not found: $srcCpp (run from repo clone; IOFv02 must exist next to IOF_NQ_Production_Final)."
}

function Assert-SameFileSize {
    param(
        [Parameter(Mandatory)][string] $SourcePath,
        [Parameter(Mandatory)][string] $DestPath
    )
    $a = Get-Item -LiteralPath $SourcePath -ErrorAction Stop
    $b = Get-Item -LiteralPath $DestPath -ErrorAction Stop
    if ($a.Length -ne $b.Length) {
        Write-Error "Copy verify failed (size mismatch): $SourcePath -> $DestPath"
    }
}

function Copy-One {
    param(
        [Parameter(Mandatory)][string] $From,
        [Parameter(Mandatory)][string] $To,
        [Parameter(Mandatory)][string] $Label
    )
    if (-not (Test-Path -LiteralPath $From)) {
        Write-Error "Missing source: $From ($Label)"
    }
    $destDir = Split-Path -Parent $To
    if ($destDir -and -not (Test-Path -LiteralPath $destDir)) {
        if ($PSCmdlet.ShouldProcess($destDir, "Create directory")) {
            New-Item -ItemType Directory -Force -Path $destDir | Out-Null
        }
    }
    if ($PSCmdlet.ShouldProcess($To, "Copy $Label")) {
        Copy-Item -LiteralPath $From -Destination $To -Force
        Assert-SameFileSize -SourcePath $From -DestPath $To
    }
}

$sw = [System.Diagnostics.Stopwatch]::StartNew()

Copy-One -From $srcCpp -To (Join-Path $here $mainCpp) -Label "main study"

if (-not $CppOnly) {
    Copy-One -From (Join-Path $src "iof_v1_hooks.h") -To (Join-Path $here "iof_v1_hooks.h") -Label "v1 hooks header"
    $udSrc = Join-Path $src "iof_unified"
    $udDst = Join-Path $here "iof_unified"
    if (-not (Test-Path -LiteralPath $udSrc)) {
        Write-Error "Missing folder: $udSrc"
    }
    if ($PSCmdlet.ShouldProcess($udDst, "Mirror iof_unified headers")) {
        New-Item -ItemType Directory -Force -Path $udDst | Out-Null
        Get-ChildItem -LiteralPath $udSrc -Filter "*.h" -File | ForEach-Object {
            Copy-One -From $_.FullName -To (Join-Path $udDst $_.Name) -Label "iof_unified\$($_.Name)"
        }
    }
}

if ($CopySierraDocs) {
    foreach ($doc in @("SIERRA_REPLAY_CHECKLIST.txt", "BUILD.txt")) {
        $p = Join-Path $src $doc
        if (Test-Path -LiteralPath $p) {
            Copy-One -From $p -To (Join-Path $here $doc) -Label $doc
        }
        else {
            Write-Warning "Optional doc not found (skipped): $p"
        }
    }
}

if (-not $NoVersionStamp) {
    $versionPath = Join-Path $here "VERSION.txt"
    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $body = @(
        "IOF_NQ_Production_Final bundle",
        "Study: IOF NQ NQ Autopilot (see header in $mainCpp)",
        "Last sync from IOFv02: $stamp",
        "Source tree: $src",
        "Sync flags: CppOnly=$CppOnly CopySierraDocs=$CopySierraDocs",
        "Purpose: Sierra Chart ACSIL - refresh before remote/local DLL build."
    ) -join [Environment]::NewLine
    if ($PSCmdlet.ShouldProcess($versionPath, "Write VERSION.txt")) {
        $utf8NoBom = New-Object System.Text.UTF8Encoding $false
        [System.IO.File]::WriteAllText($versionPath, $body + [Environment]::NewLine, $utf8NoBom)
    }
}

$sw.Stop()
if ($VerbosePreference -eq "Continue") {
    Write-Verbose ("Sync finished in {0} ms" -f $sw.ElapsedMilliseconds)
}

if ($WhatIfPreference) {
    Write-Host ('Dry run only ({0:N0} ms); no files were changed.' -f $sw.ElapsedMilliseconds)
}
else {
    Write-Host ('Synced IOF_NQ_Production_Final from IOFv02 OK ({0:N0} ms).' -f $sw.ElapsedMilliseconds)
}
