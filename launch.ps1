[CmdletBinding()]
param(
    [string]$AntigravityPath = (Join-Path $env:LOCALAPPDATA 'Programs\antigravity\Antigravity.exe'),
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$AppArguments
)

$ErrorActionPreference = 'Stop'
if ($env:AGY_CAM_SIDECAR_RELAY -eq '1') {
    throw 'Start from a fresh shell or use the restored Antigravity shortcut.'
}
& $AntigravityPath @AppArguments
