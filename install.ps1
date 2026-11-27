[CmdletBinding(DefaultParameterSetName = 'Workspace')]
param(
    [Parameter(Mandatory = $true, ParameterSetName = 'Workspace')]
    [string]$Workspace,
    [Parameter(Mandatory = $true, ParameterSetName = 'Global')]
    [switch]$Global,
    [Parameter(ParameterSetName = 'Global')]
    [string]$Python = 'python'
)

$ErrorActionPreference = 'Stop'
$pluginName = 'antigravity-context-attachment-manager'
$files = @('plugin.json', 'mcp_config.json', 'README.md', 'rules\AGENTS.md', 'src\mcp_server.py', 'src\protocol.py', 'src\archive.py', 'src\adapter.py', 'src\panel.py', 'src\native_state.py', 'src\conversation_names.py', 'src\desktop_selection.py', 'src\legacy_timeline.py', 'ui\attachments.html')

if ($Global) {
    $configRoot = Join-Path $env:USERPROFILE '.gemini\config'
    $configPath = Join-Path $configRoot 'config.json'
    $pluginDir = Join-Path $configRoot "plugins\$pluginName"
    $config = if (Test-Path -LiteralPath $configPath) {
        Get-Content -Raw -LiteralPath $configPath | ConvertFrom-Json
    } else { [pscustomobject]@{} }
    if ($config -isnot [pscustomobject]) { throw 'Unexpected Antigravity config format.' }
    foreach ($key in @('plugins', 'sidecars')) {
        if (-not $config.PSObject.Properties[$key] -or $null -eq $config.$key) {
            $config | Add-Member -NotePropertyName $key -NotePropertyValue ([pscustomobject]@{}) -Force
        } elseif ($config.$key -isnot [pscustomobject]) {
            throw "Unexpected Antigravity $key config format."
        }
    }
    $pythonExe = & $Python -c 'import sys; assert sys.version_info >= (3, 10); print(sys.executable)'
    if ($LASTEXITCODE -ne 0 -or -not $pythonExe) { throw 'Python 3.10 or newer is required.' }
    $files += @('src\native_startup.py', 'src\proxy.py', 'sidecars\startup\sidecar.json')
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

if ($Global) {
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    $mcpPath = Join-Path $pluginDir 'mcp_config.json'
    $mcpConfig = Get-Content -Raw -LiteralPath $mcpPath | ConvertFrom-Json
    $mcpConfig.mcpServers.media.command = $pythonExe
    [IO.File]::WriteAllText($mcpPath, ($mcpConfig | ConvertTo-Json -Depth 12), $utf8)

    $sidecarPath = Join-Path $pluginDir 'sidecars\startup\sidecar.json'
    $sidecar = Get-Content -Raw -LiteralPath $sidecarPath | ConvertFrom-Json
    $sidecar.command = $pythonExe
    $sidecar.args = @((Join-Path $pluginDir 'src\native_startup.py'))
    [IO.File]::WriteAllText($sidecarPath, ($sidecar | ConvertTo-Json -Depth 12), $utf8)

    $components = @(@{ group = 'plugins'; name = $pluginName }, @{ group = 'sidecars'; name = "$pluginName/startup" })
    foreach ($entry in $components) {
        $group = $config.($entry.group)
        $name = $entry.name
        if (-not $group.PSObject.Properties[$name] -or $null -eq $group.$name) {
            $group | Add-Member -NotePropertyName $name -NotePropertyValue ([pscustomobject]@{}) -Force
        }
        $group.$name | Add-Member -NotePropertyName 'enabled' -NotePropertyValue $true -Force
    }
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
