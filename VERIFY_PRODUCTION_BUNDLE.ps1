<#
.SYNOPSIS
  Validate production bundle naming consistency for Sierra ACSIL build.

.DESCRIPTION
  Checks that:
    - Canonical source file exists (IOF_NQ_Autopilot.cpp)
    - SCDLLName("...") is present
    - SCDLLName equals expected production DLL base
  Prints expected DLL name for 64-bit Sierra.
#>
[CmdletBinding()]
param()

Set-StrictMode -Version 3.0
$ErrorActionPreference = "Stop"

$here = $PSScriptRoot
$cppName = "IOF_NQ_Autopilot.cpp"
$expectedDllBase = "IOF_NQ_Autopilot"
$cppPath = Join-Path $here $cppName
if (-not (Test-Path -LiteralPath $cppPath)) {
    throw "Missing canonical source: $cppPath"
}

$content = Get-Content -LiteralPath $cppPath -Raw
$m = [regex]::Match($content, 'SCDLLName\("([^"]+)"\)')
if (-not $m.Success) {
    throw "SCDLLName(""..."") not found in $cppName"
}

$dllBaseFromMacro = $m.Groups[1].Value
if ($dllBaseFromMacro -ne $expectedDllBase) {
    throw "Mismatch: expected SCDLLName '$expectedDllBase' but found '$dllBaseFromMacro'"
}

$expectedDll = "$expectedDllBase" + "_64.dll"
Write-Host "OK: canonical source + SCDLLName match production target: $expectedDllBase"
Write-Host "Expected Sierra 64-bit DLL name: $expectedDll"
Write-Host "Compile only: $cppName"
