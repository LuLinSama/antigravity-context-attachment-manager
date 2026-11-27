[CmdletBinding()]
param(
    [string]$Shortcut = (Join-Path ([Environment]::GetFolderPath('DesktopDirectory')) 'Antigravity.lnk'),
    [switch]$Disable
)

$ErrorActionPreference = 'Stop'
$path = (Resolve-Path -LiteralPath $Shortcut).ProviderPath
if ([IO.Path]::GetExtension($path) -ine '.lnk') { throw 'Expected a Windows shortcut.' }
$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut($path)
if ([IO.Path]::GetFileName($link.TargetPath) -ine 'cmd.exe') {
    throw 'Expected an existing cmd.exe shortcut that starts Antigravity. The target was left untouched.'
}

$wrapper = [regex]::Match($link.Arguments, '(?i)^(?<prefix>/d\s+/s\s+/c\s+")(?<body>.+)"$')
if (-not $wrapper.Success) { throw 'The shortcut uses an unsupported command wrapper.' }
$body = $wrapper.Groups['body'].Value
$managed = [regex]::Match($body, '^set "CLOUD_CODE_URL=http://127\.0\.0\.1:(?<port>[0-9]{1,5})/cam-(?<key>[a-f0-9]{32})" && set "AGY_CAM_SIDECAR_RELAY=1" && ')
$original = if ($managed.Success) { $body.Substring($managed.Length) } else { $body }
if ($original -notmatch '(?i)(?:^|&&\s*)start\s.*[\\/]Antigravity\.exe"(?:\s|$)' -or
    $original -match '(?i)\b(?:CLOUD_CODE_URL|AGY_CAM_SIDECAR_RELAY)\s*=') {
    throw 'The shortcut does not start Antigravity or already sets a conflicting variable.'
}

if ($Disable) {
    if (-not $managed.Success) { Write-Host 'The shortcut already uses the original routing.'; return }
    $link.Arguments = $wrapper.Groups['prefix'].Value + $original + '"'
    $link.Save()
    Write-Host "Removed the two attachment variables from $path"
    return
}
if ($managed.Success) { Write-Host 'The shortcut already supplies the attachment relay address.'; return }

$sidecar = Join-Path $env:USERPROFILE '.gemini\config\plugins\antigravity-context-attachment-manager\src\native_startup.py'
if (-not (Test-Path -LiteralPath $sidecar -PathType Leaf) -or
    -not (Select-String -LiteralPath $sidecar -SimpleMatch 'AGY_CAM_SIDECAR_RELAY' -Quiet)) {
    throw 'Install the updated global Sidecar first with install.ps1 -Global.'
}

$socket = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
try {
    $socket.Start()
    $port = ([Net.IPEndPoint]$socket.LocalEndpoint).Port
} finally { $socket.Stop() }
$key = [Guid]::NewGuid().ToString('N')
$settings = 'set "CLOUD_CODE_URL=http://127.0.0.1:' + $port + '/cam-' + $key + '" && set "AGY_CAM_SIDECAR_RELAY=1" && '
$backupDir = Join-Path $env:LOCALAPPDATA 'antigravity-context-attachment-manager'
New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
$backup = Join-Path $backupDir ('Antigravity.before-routing-' + [DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss-ffff') + '.lnk')
Copy-Item -LiteralPath $path -Destination $backup
$link.Arguments = $wrapper.Groups['prefix'].Value + $settings + $original + '"'
$link.Save()
Write-Host "Added two temporary variables to $path"
Write-Host "Original shortcut backup: $backup"
