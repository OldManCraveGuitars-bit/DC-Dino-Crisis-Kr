[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$SourceChd,
    [string]$OutputChd,
    [string]$Chdman = 'chdman',
    [string]$Xdelta3 = 'xdelta3'
)

$ErrorActionPreference = 'Stop'
$manifest = Get-Content -Raw -LiteralPath (Join-Path $PSScriptRoot 'manifest.json') | ConvertFrom-Json
$patch = Join-Path $PSScriptRoot $manifest.patch.name

function Assert-FileHash([string]$Path, $Expected) {
    $file = Get-Item -LiteralPath $Path
    if ($file.Length -ne $Expected.bytes -or
        (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash -ne $Expected.sha256) {
        throw "Unexpected size or SHA-256 for $Path. Expected $($Expected.sha256)."
    }
}

function Invoke-Tool([string]$ToolPath, [string[]]$Arguments) {
    & $ToolPath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$ToolPath exited with code $LASTEXITCODE."
    }
}

function Convert-Mode1Track([string]$RawPath, [string]$IsoPath) {
    $inputTrack = [System.IO.BinaryReader]::new([System.IO.File]::OpenRead($RawPath))
    try {
        $outputTrack = [System.IO.File]::Open($IsoPath, 'CreateNew', 'Write')
        try {
            $cooked = [byte[]]::new(2048 * 4096)
            while ($true) {
                $raw = $inputTrack.ReadBytes(2352 * 4096)
                if ($raw.Length -eq 0) { break }
                if ($raw.Length % 2352 -ne 0) { throw 'Truncated raw track.' }
                $sectors = $raw.Length / 2352
                for ($i = 0; $i -lt $sectors; $i++) {
                    [System.Buffer]::BlockCopy($raw, $i * 2352 + 16, $cooked, $i * 2048, 2048)
                }
                $outputTrack.Write($cooked, 0, $sectors * 2048)
            }
        }
        finally { $outputTrack.Dispose() }
    }
    finally { $inputTrack.Dispose() }
}

$original = (Get-Item -LiteralPath $SourceChd).FullName
Assert-FileHash $original $manifest.source
Assert-FileHash $patch $manifest.patch
$chdmanExe = (Get-Command $Chdman -CommandType Application).Source
$xdeltaExe = (Get-Command $Xdelta3 -CommandType Application).Source

if (-not $OutputChd) {
    $OutputChd = Join-Path (Split-Path -Parent $original) $manifest.target.name
}
$output = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutputChd)
if (Test-Path -LiteralPath $output) {
    throw "The output file already exists: $output."
}
$parent = Split-Path -Parent $output
if (-not (Test-Path -LiteralPath $parent -PathType Container)) {
    throw "The output directory does not exist: $parent."
}
$working = Join-Path $parent ('.dino-v08-' + [guid]::NewGuid().ToString('N'))
New-Item -Path $working -ItemType Directory | Out-Null

try {
    $sourceGdi = Join-Path $working 'source.gdi'
    $sourceTrack = Join-Path $working 'source03.bin'
    $patchedTrack = Join-Path $working 'patched03.iso'
    $patchedGdi = Join-Path $working 'patched.gdi'
    $patchedChd = Join-Path $working 'patched.chd'

    Write-Host 'Extracting the Japanese CHD...'
    Invoke-Tool $chdmanExe @('extractcd', '-i', $original, '-o', $sourceGdi)
    $track = (Get-Content -LiteralPath $sourceGdi)[3].Trim() -split '\s+'
    if ($track[0] -ne '3' -or $track[1] -ne '45000' -or $track[4] -ne 'source03.bin' -or $track[5] -ne '0') {
        throw 'Unexpected source GDI layout.'
    }
    if ($track[3] -eq '2352') {
        $cookedTrack = Join-Path $working 'source03.iso'
        Convert-Mode1Track $sourceTrack $cookedTrack
        $sourceTrack = $cookedTrack
    }
    elseif ($track[3] -ne '2048') {
        throw "Unsupported sector size: $($track[3])."
    }
    Assert-FileHash $sourceTrack $manifest.track03_source

    Write-Host 'Applying the track 3 patch...'
    Invoke-Tool $xdeltaExe @('-d', '-s', $sourceTrack, $patch, $patchedTrack)
    Assert-FileHash $patchedTrack $manifest.track03_target

    @(
        '3'
        '1 0 4 2352 source01.bin 0'
        '2 600 0 2352 source02.raw 0'
        '3 45000 4 2048 patched03.iso 0'
    ) | Set-Content -LiteralPath $patchedGdi -Encoding ASCII

    Write-Host 'Creating and verifying the Korean CHD...'
    # chdman 0.264's GDI reader needs relative ASCII names in a Unicode directory.
    Push-Location -LiteralPath $working
    try {
        Invoke-Tool $chdmanExe @('createcd', '-i', 'patched.gdi', '-o', 'patched.chd')
    }
    finally { Pop-Location }
    Invoke-Tool $chdmanExe @('verify', '-i', $patchedChd)
    Assert-FileHash $patchedChd $manifest.target
    [System.IO.File]::Move($patchedChd, $output)
    Write-Host "Created $output"
    Write-Host "SHA-256 $($manifest.target.sha256)"
}
finally {
    Get-ChildItem -LiteralPath $working -File | ForEach-Object { Remove-Item -LiteralPath $_.FullName }
    [System.IO.Directory]::Delete($working, $false)
}
