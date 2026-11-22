[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Delta,
    [Parameter(Mandatory)][string]$Chdman,
    [Parameter(Mandatory)][string]$Xdelta3,
    [string]$Manifest,
    [string]$BuildDirectory
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
if (-not $Manifest) { $Manifest = Join-Path $root 'manifest.json' }
if (-not $BuildDirectory) { $BuildDirectory = Join-Path $root 'build' }
New-Item -ItemType Directory -Path $BuildDirectory -Force | Out-Null
$build = (Resolve-Path -LiteralPath $BuildDirectory).Path
$payload = Join-Path $build 'payload'
New-Item -ItemType Directory -Path $payload -Force | Out-Null

$manifestPath = (Resolve-Path -LiteralPath $Manifest).Path
$hashesPath = Join-Path $root 'patcher/tool-hashes.json'
$release = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
$hashes = Get-Content -LiteralPath $hashesPath -Raw | ConvertFrom-Json
foreach ($pair in @(
    @{ source = $Delta; expected = $release.patch }
    @{ source = $Chdman; expected = $hashes.chdman }
    @{ source = $Xdelta3; expected = $hashes.xdelta }
)) {
    $file = Get-Item -LiteralPath $pair.source
    if ($file.Length -ne $pair.expected.bytes -or
        (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash -ne $pair.expected.sha256) {
        throw "Unexpected input SHA-256 or size: $($file.FullName)"
    }
    Copy-Item -LiteralPath $file.FullName -Destination (Join-Path $payload $pair.expected.name)
}
Copy-Item -LiteralPath $manifestPath -Destination (Join-Path $payload 'manifest.json')
Copy-Item -LiteralPath $hashesPath -Destination (Join-Path $payload 'tool-hashes.json')
$arguments = @(
    '--noconfirm', '--clean', '--onefile', '--windowed', '--noupx',
    '--name', 'DC-Dino-Crisis-Kr-v0.81-Patcher',
    '--version-file', (Join-Path $root 'patcher/version_info.txt'),
    '--add-data', "$(Join-Path $root 'licenses');licenses",
    '--distpath', (Join-Path $build 'dist'),
    '--workpath', (Join-Path $build 'pyinstaller'),
    '--specpath', $build
)
foreach ($name in @($release.patch.name, $hashes.chdman.name, $hashes.xdelta.name, 'manifest.json', 'tool-hashes.json')) {
    $arguments += @('--add-data', "$(Join-Path $payload $name);payload")
}
$arguments += (Join-Path $root 'patcher/patcher.py')
& python -m PyInstaller @arguments
if ($LASTEXITCODE -ne 0) { throw 'EXE patcher build failed.' }
