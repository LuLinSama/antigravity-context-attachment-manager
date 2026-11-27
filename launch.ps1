[CmdletBinding()]
param(
    [string]$AntigravityPath = (Join-Path $env:LOCALAPPDATA 'Programs\antigravity\Antigravity.exe'),
    [string]$Python = 'python',
    [string]$DataDirectory,
    [switch]$Observe,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$AppArguments
)

$ErrorActionPreference = 'Stop'
if ([IO.Path]::GetFileName($AntigravityPath) -ieq 'Antigravity.exe' -and (Get-Process -Name Antigravity -ErrorAction SilentlyContinue)) {
    throw 'Close the running Antigravity App before using this launcher.'
}
$proxyArguments = @((Join-Path $PSScriptRoot 'src\proxy.py'), '--app', $AntigravityPath)
if ($DataDirectory) {
    $proxyArguments += @('--data-dir', $DataDirectory)
}
if ($Observe) {
    $proxyArguments += '--observe'
}
if ($AppArguments) {
    $proxyArguments += '--'
    $proxyArguments += $AppArguments
}
& $Python @proxyArguments
exit $LASTEXITCODE
