[CmdletBinding(SupportsShouldProcess, DefaultParameterSetName = 'Workspace')]
param(
    [Parameter(Mandatory = $true, ParameterSetName = 'Workspace')]
    [string]$Workspace,
    [Parameter(Mandatory = $true, ParameterSetName = 'Global')]
    [switch]$Global
)

$ErrorActionPreference = 'Stop'
$pluginName = 'antigravity-context-attachment-manager'
if ($PSCmdlet.ParameterSetName -eq 'Global' -and -not $Global) { throw 'Specify -Global to uninstall globally.' }

function Get-PlainItem([string]$Path) {
    try {
        $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    } catch [System.Management.Automation.ItemNotFoundException] {
        return
    }
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "Refusing to follow a link or junction: $Path"
    }
    return $item
}

function Get-PlainDirectory([string]$Path) {
    # Resolve-Path alone does not reject junctions. Check from the drive down.
    $ancestors = [System.Collections.Generic.Stack[string]]::new()
    for ($parent = [IO.DirectoryInfo]::new($Path).Parent; $null -ne $parent; $parent = $parent.Parent) {
        $ancestors.Push($parent.FullName)
    }
    while ($ancestors.Count -gt 0) {
        $ancestorPath = $ancestors.Pop()
        $ancestor = Get-PlainItem $ancestorPath
        if ($null -ne $ancestor -and $ancestor -isnot [IO.DirectoryInfo]) {
            throw "Expected a directory: $ancestorPath"
        }
    }
    $item = Get-PlainItem $Path
    if ($null -ne $item -and ($item -isnot [IO.DirectoryInfo] -or
        -not [string]::Equals($item.FullName, $Path, [StringComparison]::OrdinalIgnoreCase))) {
        throw "Unexpected directory path: $Path"
    }
    return $item
}

function Assert-PlainTree([IO.DirectoryInfo]$Directory) {
    $pending = [System.Collections.Generic.Stack[IO.DirectoryInfo]]::new()
    $pending.Push($Directory)
    while ($pending.Count -gt 0) {
        foreach ($item in $pending.Pop().EnumerateFileSystemInfos()) {
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "Refusing to remove a directory containing a link or junction: $($item.FullName)"
            }
            if ($item -is [IO.DirectoryInfo]) { $pending.Push($item) }
        }
    }
}

function Read-JsonObject([string]$Path) {
    $raw = [IO.File]::ReadAllText($Path)
    if (-not $raw.TrimStart().StartsWith('{')) { throw "Expected a JSON object: $Path" }
    $object = ConvertFrom-Json -InputObject $raw
    if ($object -isnot [pscustomobject]) { throw "Expected a JSON object: $Path" }
    return $object
}

if ($env:USERPROFILE -notmatch '\A[A-Za-z]:[\\/]') { throw 'Expected an absolute USERPROFILE path.' }
$root = if ($PSCmdlet.ParameterSetName -eq 'Global') { $env:USERPROFILE } else { $Workspace }
$root = Resolve-Path -LiteralPath $root
if ($root.Provider.Name -ne 'FileSystem' -or -not (Test-Path -LiteralPath $root.ProviderPath -PathType Container)) {
    throw 'Installation root must be an existing filesystem directory.'
}

$relative = if ($PSCmdlet.ParameterSetName -eq 'Global') { '.gemini\config\plugins' } else { '.agents\plugins' }
$pluginDir = [IO.Path]::GetFullPath((Join-Path (Join-Path $root.ProviderPath $relative) $pluginName))

$installed = Get-PlainDirectory $pluginDir
$emptyDirectory = $false
if ($null -ne $installed) {
    $manifestPath = Join-Path $installed.FullName 'plugin.json'
    $manifestFile = Get-PlainItem $manifestPath
    if ($null -eq $manifestFile -and
        $null -eq (Get-ChildItem -LiteralPath $installed.FullName -Force | Select-Object -First 1)) {
        $emptyDirectory = $true
    } else {
        if ($manifestFile -isnot [IO.FileInfo]) {
            throw "Non-empty plugin directory has no valid plugin.json; refusing to remove: $pluginDir"
        }
        $manifest = Read-JsonObject $manifestPath
        if ($manifest.name -cne $pluginName) {
            throw "Plugin manifest does not identify CAM; refusing to remove: $pluginDir"
        }
        Assert-PlainTree $installed
    }
}

# Match src/native_state.py: both installation scopes share this per-user directory.
$localAppData = $env:LOCALAPPDATA
if ([string]::IsNullOrEmpty($localAppData)) {
    $localAppData = Join-Path $env:USERPROFILE 'AppData\Local'
}
if ($localAppData -notmatch '\A[A-Za-z]:[\\/]') { throw 'Expected an absolute LOCALAPPDATA path.' }
$dataDir = [IO.Path]::GetFullPath((Join-Path $localAppData $pluginName))
# A misconfigured LOCALAPPDATA must not turn uninstall into deletion of the source checkout.
if ($PSScriptRoot.StartsWith($dataDir + '\', [StringComparison]::OrdinalIgnoreCase) -or
    [string]::Equals($PSScriptRoot, $dataDir, [StringComparison]::OrdinalIgnoreCase)) {
    throw "CAM data directory overlaps the uninstall script: $dataDir"
}
$data = Get-PlainDirectory $dataDir
if ($null -ne $data) { Assert-PlainTree $data }

# Antigravity stores these runtime artifacts by plugin ID, not by installation scope.
$runtimeRoot = [IO.Path]::GetFullPath((Join-Path $env:USERPROFILE '.gemini\antigravity'))
$runtimeDirs = @(
    foreach ($relativePath in @("mcp\${pluginName}_media", "mcp\${pluginName}_attachments",
                                "plugin_data\$pluginName", "sidecar_data\$pluginName")) {
        $directory = Get-PlainDirectory (Join-Path $runtimeRoot $relativePath)
        if ($null -ne $directory) {
            Assert-PlainTree $directory
            $directory
        }
    }
)

$registrations = @()
$staleTemps = @()
if ($PSCmdlet.ParameterSetName -eq 'Global') {
    $configRoot = Join-Path $root.ProviderPath '.gemini\config'
    $configPath = Join-Path $configRoot 'config.json'
    $configFile = Get-PlainItem $configPath
    if ($null -ne $configFile) {
        if ($configFile -isnot [IO.FileInfo]) { throw "Unexpected configuration path: $configPath" }
        $config = Read-JsonObject $configPath
        if ($null -ne $config.plugins -and $config.plugins -isnot [pscustomobject]) {
            throw 'Unexpected Antigravity plugins config format.'
        }
        # Older CAM installers registered a startup sidecar under this exact name.
        foreach ($entry in @(@{ Group = $config.plugins; Name = $pluginName },
                             @{ Group = $config.sidecars; Name = "$pluginName/startup" })) {
            if ($entry.Group -isnot [pscustomobject]) { continue }
            $property = $entry.Group.PSObject.Properties[$entry.Name]
            if ($property -and $property.Name -ceq $entry.Name) { $registrations += $entry }
        }
    }
    if ($null -ne (Get-PlainItem $configRoot)) {
        # Only match temp files named by CAM's atomic config writer.
        $staleTemps = @(Get-ChildItem -LiteralPath $configRoot -Force -File -Filter '.cam-*.tmp' |
            Where-Object { $_.Name -cmatch '\A\.cam-[0-9a-f]{32}\.tmp\z' } |
            ForEach-Object { Get-PlainItem $_.FullName })
    }
}

$targets = @()
if ($null -ne $installed) { $targets += $pluginDir }
if ($registrations.Count -gt 0) { $targets += "CAM registrations in $configPath" }
if ($staleTemps.Count -gt 0) { $targets += $staleTemps.FullName }
if ($runtimeDirs.Count -gt 0) { $targets += $runtimeDirs.FullName }
if ($null -ne $data) { $targets += "$dataDir (shared CAM data)" }
if ($targets.Count -eq 0) {
    Write-Host 'No CAM installation or local data found for this uninstall.'
    return
}
if (-not $PSCmdlet.ShouldProcess(($targets -join '; '), 'Uninstall Context Attachment Manager')) { return }

if ($emptyDirectory) {
    # Fail rather than deleting any files that appeared after the check above.
    [IO.Directory]::Delete($installed.FullName, $false)
} elseif ($null -ne $installed) {
    Remove-Item -LiteralPath $installed.FullName -Recurse -Force -Confirm:$false
}
if ($registrations.Count -gt 0) {
    foreach ($entry in $registrations) { $entry.Group.PSObject.Properties.Remove($entry.Name) }
    $temp = Join-Path $configRoot ('.cam-' + [Guid]::NewGuid() + '.tmp')
    try {
        [IO.File]::WriteAllText($temp, ($config | ConvertTo-Json -Depth 100), [Text.UTF8Encoding]::new($false))
        [IO.File]::Replace($temp, $configPath, [NullString]::Value)
    } finally {
        if (Test-Path -LiteralPath $temp) { Remove-Item -LiteralPath $temp }
    }
}
foreach ($file in $staleTemps) { Remove-Item -LiteralPath $file.FullName -Force -Confirm:$false }
foreach ($directory in $runtimeDirs) {
    Remove-Item -LiteralPath $directory.FullName -Recurse -Force -Confirm:$false
    Write-Host "Removed CAM runtime files: $($directory.FullName)"
}
if ($null -ne $data) {
    Remove-Item -LiteralPath $data.FullName -Recurse -Force -Confirm:$false
    Write-Host "Removed shared CAM data: $dataDir"
}
Write-Host 'CAM uninstall completed for the selected scope.'
