[CmdletBinding(DefaultParameterSetName = 'Workspace')]
param(
    [Parameter(Mandatory = $true, ParameterSetName = 'Workspace')]
    [string]$Workspace,
    [Parameter(Mandatory = $true, ParameterSetName = 'Global')]
    [switch]$Global,
    [string]$Python = 'python'
)

$ErrorActionPreference = 'Stop'
$pluginName = 'antigravity-context-attachment-manager'
$files = @(
    'plugin.json', 'mcp_config.json', 'hooks.json', 'README.md', 'LICENSE',
    'scripts\record_read.py', 'src\mcp_server.py', 'src\archive.py', 'src\media.py',
    'src\panel.py', 'src\native_state.py', 'src\conversation_names.py',
    'src\desktop_selection.py', 'src\legacy_timeline.py', 'ui\attachments.html'
)
$pythonExe = & $Python -c 'import sys; assert sys.version_info >= (3, 10); print(sys.executable)'
if ($LASTEXITCODE -ne 0 -or -not $pythonExe) { throw 'Python 3.10 or newer is required.' }
$utf8 = New-Object System.Text.UTF8Encoding($false)

if ($Global) {
    $configRoot = Join-Path $env:USERPROFILE '.gemini\config'
    $configPath = Join-Path $configRoot 'config.json'
    $pluginDir = Join-Path $configRoot "plugins\$pluginName"
    $config = if (Test-Path -LiteralPath $configPath) {
        Get-Content -Raw -LiteralPath $configPath | ConvertFrom-Json
    } else { [pscustomobject]@{} }
    if ($config -isnot [pscustomobject]) { throw 'Unexpected Antigravity config format.' }
    if (-not $config.PSObject.Properties['plugins'] -or $null -eq $config.plugins) {
        $config | Add-Member -NotePropertyName plugins -NotePropertyValue ([pscustomobject]@{}) -Force
    } elseif ($config.plugins -isnot [pscustomobject]) {
        throw 'Unexpected Antigravity plugins config format.'
    }
    if (-not $config.plugins.PSObject.Properties[$pluginName] -or $null -eq $config.plugins.$pluginName) {
        $config.plugins | Add-Member -NotePropertyName $pluginName -NotePropertyValue ([pscustomobject]@{}) -Force
    }
    $config.plugins.$pluginName | Add-Member -NotePropertyName enabled -NotePropertyValue $true -Force
    $oldStartup = "$pluginName/startup"
    if ($config.sidecars -and $config.sidecars.PSObject.Properties[$oldStartup]) {
        $config.sidecars.PSObject.Properties.Remove($oldStartup)
    }
    $shortcut = Join-Path ([Environment]::GetFolderPath('DesktopDirectory')) 'Antigravity.lnk'
    if (Test-Path -LiteralPath $shortcut -PathType Leaf) {
        & (Join-Path $PSScriptRoot 'configure-shortcut.ps1') -Shortcut $shortcut -Disable
    }
} else {
    $workspacePath = (Resolve-Path -LiteralPath $Workspace).ProviderPath
    if (-not (Test-Path -LiteralPath $workspacePath -PathType Container)) {
        throw 'Workspace must be an existing directory.'
    }
    $pluginDir = Join-Path $workspacePath ".agents\plugins\$pluginName"
}

foreach ($entry in $files) {
    $destination = Join-Path $pluginDir $entry
    New-Item -ItemType Directory -Path (Split-Path -LiteralPath $destination) -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot $entry) -Destination $destination -Force
}

$mcpPath = Join-Path $pluginDir 'mcp_config.json'
$mcpConfig = Get-Content -Raw -LiteralPath $mcpPath | ConvertFrom-Json
$mcpConfig.mcpServers.media.command = $pythonExe
[IO.File]::WriteAllText($mcpPath, ($mcpConfig | ConvertTo-Json -Depth 12), $utf8)

$hooksPath = Join-Path $pluginDir 'hooks.json'
$hooksConfig = Get-Content -Raw -LiteralPath $hooksPath | ConvertFrom-Json
$hooksConfig.'cam-file-reads'.PostToolUse[0].hooks[0].command = '"' + $pythonExe + '" ./scripts/record_read.py'
[IO.File]::WriteAllText($hooksPath, ($hooksConfig | ConvertTo-Json -Depth 12), $utf8)

foreach ($entry in @('rules\AGENTS.md', 'sidecars\startup\sidecar.json', 'src\proxy.py', 'src\native_startup.py', 'src\desktop.py', 'src\adapter.py', 'src\protocol.py')) {
    $staleFile = Join-Path $pluginDir $entry
    if (Test-Path -LiteralPath $staleFile -PathType Leaf) { Remove-Item -LiteralPath $staleFile }
}

if ($Global) {
    $temp = Join-Path $configRoot ('.cam-' + [Guid]::NewGuid() + '.tmp')
    try {
        [IO.File]::WriteAllText($temp, ($config | ConvertTo-Json -Depth 100), $utf8)
        if (Test-Path -LiteralPath $configPath) {
            [IO.File]::Replace($temp, $configPath, [NullString]::Value)
        } else { [IO.File]::Move($temp, $configPath) }
    } finally {
        if (Test-Path -LiteralPath $temp) { Remove-Item -LiteralPath $temp }
    }
}
Write-Host "Installed Context Attachment Manager in $pluginDir"
