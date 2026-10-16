# Fetch, verify and stage the official Codex CLI release for Windows x86_64.
#
# This is the single source of truth for the Windows Codex version and release
# layout used by misc/vibe_agent/package_windows.ps1. Only Windows x86_64 is
# supported: the script downloads the official x86_64-pc-windows-msvc package
# from releases.openai.com (with an official GitHub Releases fallback), verifies
# the SHA-256 manifest before extracting, and validates the extracted layout,
# the ``codex.exe --version`` output and the PE machine type.
#
# Usage:
#   fetch_codex_windows.ps1
#       Fetch (or reuse) the pinned Codex release and print the absolute path of
#       the prepared release directory on stdout. Diagnostics go to the
#       PowerShell warning stream or stderr.
#   fetch_codex_windows.ps1 -Validate -ReleaseRoot DIR [-Version VERSION]
#       Verify an extracted release layout and print the reported Codex version.
#       VERSION defaults to the version recorded in codex-package.json.
#
# Environment overrides (verification is never weakened):
#   CODEX_VERSION       Release version to fetch (default: 0.146.0).
#   CODEX_RELEASE_ROOT  Reuse this already-extracted release layout instead of
#                       downloading it.
#   CODEX_BIN           Reuse the release layout that contains this codex.exe
#                       instead of downloading it.
#
# Downloads and build output stay under the repository .build/codex directory.
# Archives, credentials, OAuth tokens and logs containing secrets are never
# written or committed, and no token, model or working directory is ever passed
# through the command line.

[CmdletBinding()]
param(
    [string]$Version = "",
    [string]$ReleaseRoot = "",
    [string]$Bin = "",
    [string]$BuildRoot = "",
    [switch]$Validate
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

# GitHub and releases.openai.com require TLS 1.2, which is not always enabled by
# default in Windows PowerShell 5.1.
try {
    $currentProtocols = [Net.ServicePointManager]::SecurityProtocol
    if (($currentProtocols -band [Net.SecurityProtocolType]::Tls12) -eq 0) {
        [Net.ServicePointManager]::SecurityProtocol = $currentProtocols -bor [Net.SecurityProtocolType]::Tls12
    }
} catch {
}

$script:defaultCodexVersion = "0.146.0"
$script:codexTarget = "x86_64-pc-windows-msvc"
$script:codexAsset = "codex-package-$($script:codexTarget).tar.gz"
$script:codexChecksumAsset = "codex-package_SHA256SUMS"
$script:releasesBaseUrl = "https://releases.openai.com/codex"
$script:githubBaseUrl = "https://github.com/openai/codex/releases/download"
$script:tempDir = $null
$script:stageDir = $null

function Write-Diagnostic {
    param([string]$Message)
    [Console]::Error.WriteLine("fetch_codex_windows: $Message")
}

function Remove-Staging {
    foreach ($path in @($script:tempDir, $script:stageDir)) {
        if ($path -and (Test-Path -LiteralPath $path)) {
            try {
                Remove-Item -LiteralPath $path -Recurse -Force -ErrorAction SilentlyContinue
            } catch {
            }
        }
    }
}

function Die {
    param([string]$Message)
    Remove-Staging
    [Console]::Error.WriteLine("fetch_codex_windows: error: $Message")
    exit 1
}

function Test-VersionFormat {
    param([string]$Value)
    if (-not $Value) {
        return $false
    }
    return [bool]($Value -match '^[0-9]+\.[0-9]+\.[0-9]+([-+][0-9A-Za-z.]+)?$')
}

function Assert-WindowsX64 {
    if ([System.Environment]::OSVersion.Platform -ne [System.PlatformID]::Win32NT) {
        Die "Codex packaging is only supported on Windows"
    }
    $arch = $env:PROCESSOR_ARCHITECTURE
    if ($env:PROCESSOR_ARCHITEW6432) {
        $arch = $env:PROCESSOR_ARCHITEW6432
    }
    if ($arch -ne "AMD64") {
        Die "Codex packaging requires 64-bit Windows (x86_64); found $arch (Windows ARM64 is not supported)"
    }
}

function Get-TarPath {
    $candidates = @()
    if ($env:SystemRoot) {
        $candidates += (Join-Path $env:SystemRoot "System32/tar.exe")
        $candidates += (Join-Path $env:SystemRoot "Sysnative/tar.exe")
    }
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return $candidate
        }
    }
    $command = Get-Command tar.exe -ErrorAction SilentlyContinue
    if ($command -and $command.Source) {
        return $command.Source
    }
    Die "tar.exe not found; Windows 10 (1803+) or later ships tar.exe and is required to extract $($script:codexAsset)"
}

function Resolve-ReleaseRootFromBin {
    param([string]$Binary)
    if (-not (Test-Path -LiteralPath $Binary -PathType Leaf)) {
        Die "CODEX_BIN is not a file: $Binary"
    }
    $resolved = (Resolve-Path -LiteralPath $Binary).Path
    $binDir = Split-Path -Parent $resolved
    if ((Split-Path -Leaf $binDir) -eq "bin") {
        return (Split-Path -Parent $binDir)
    }
    return $binDir
}

function Get-RemoteFile {
    param(
        [string]$Url,
        [string]$Destination
    )
    Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $Destination -TimeoutSec 300
}

function Get-RemoteFileWithFallback {
    param(
        [string]$Primary,
        [string]$Fallback,
        [string]$Destination
    )
    try {
        Get-RemoteFile -Url $Primary -Destination $Destination
        return
    } catch {
        Write-Warning "download failed: $Primary"
    }
    Write-Warning "retrying from GitHub Releases"
    Get-RemoteFile -Url $Fallback -Destination $Destination
}

# Extract the SHA-256 digest for an asset from codex-package_SHA256SUMS. The
# manifest is plain text in <digest>  <asset> form, but JSON-style entries are
# accepted as well so a layout change never silently skips verification.
function Get-ManifestDigest {
    param(
        [string]$ManifestPath,
        [string]$Asset
    )
    if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
        return $null
    }
    $raw = Get-Content -LiteralPath $ManifestPath -Raw
    if (-not $raw) {
        return $null
    }
    $escaped = [regex]::Escape($Asset)
    $patterns = @(
        "([0-9a-fA-F]{64})\s+\*?$escaped",
        "$escaped[`"']?\s*[:=]\s*[`"']?([0-9a-fA-F]{64})"
    )
    foreach ($pattern in $patterns) {
        $match = [regex]::Match($raw, $pattern)
        if ($match.Success) {
            return $match.Groups[1].Value.ToLowerInvariant()
        }
    }
    return $null
}

function Read-PackageManifest {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $null
    }
    try {
        return (Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json)
    } catch {
        Write-Warning "cannot parse codex-package.json at $Path"
        return $null
    }
}

function Get-ManifestField {
    param(
        $Manifest,
        [string]$Name
    )
    if ($null -eq $Manifest) {
        return ""
    }
    if ($Manifest.PSObject.Properties.Name -contains $Name) {
        return [string]$Manifest.$Name
    }
    return ""
}

function Get-CodexVersion {
    param([string]$Binary)
    $output = $null
    try {
        $output = & $Binary --version 2>$null
    } catch {
        return $null
    }
    if ($LASTEXITCODE -ne 0) {
        return $null
    }
    if ($null -eq $output) {
        return $null
    }
    $text = (($output | ForEach-Object { [string]$_ }) -join " ").Trim()
    if (-not $text) {
        return $null
    }
    $tokens = $text -split "\s+"
    return $tokens[$tokens.Count - 1]
}

# Read the COFF machine field from the PE header. Windows x86_64 executables
# report 0x8664; anything else (for example 0xAA64 for ARM64) is rejected.
function Get-PeMachine {
    param([string]$Path)
    $stream = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::Read)
    try {
        if ($stream.Length -lt 0x40) {
            throw "file is too small to be a PE image"
        }
        $reader = New-Object -TypeName System.IO.BinaryReader -ArgumentList $stream
        try {
            if ($reader.ReadUInt16() -ne 0x5A4D) {
                throw "missing MZ signature"
            }
            $stream.Position = 0x3C
            $peOffset = $reader.ReadInt32()
            if ($peOffset -lt 0x40 -or ($peOffset + 6) -gt $stream.Length) {
                throw "invalid PE header offset"
            }
            $stream.Position = $peOffset
            if ($reader.ReadUInt32() -ne 0x00004550) {
                throw "missing PE signature"
            }
            return $reader.ReadUInt16()
        } finally {
            $reader.Dispose()
        }
    } finally {
        $stream.Dispose()
    }
}

# Verify an extracted release layout. Returns the reported version on success and
# $null on failure, writing the reason to the warning stream. Never returns an
# unverified layout.
function Test-CodexLayout {
    param(
        [string]$Layout,
        [string]$ExpectedVersion
    )

    if (-not (Test-Path -LiteralPath $Layout -PathType Container)) {
        Write-Warning "release directory not found: $Layout"
        return $null
    }

    $binary = Join-Path $Layout "bin/codex.exe"
    if (-not (Test-Path -LiteralPath $binary -PathType Leaf)) {
        Write-Warning "missing bin/codex.exe in $Layout"
        return $null
    }

    foreach ($entry in @("codex-resources", "codex-path")) {
        if (-not (Test-Path -LiteralPath (Join-Path $Layout $entry))) {
            Write-Warning "missing $entry in $Layout"
            return $null
        }
    }

    $packageJson = Join-Path $Layout "codex-package.json"
    if (-not (Test-Path -LiteralPath $packageJson -PathType Leaf)) {
        Write-Warning "missing codex-package.json in $Layout"
        return $null
    }

    try {
        $machine = Get-PeMachine -Path $binary
    } catch {
        Write-Warning "cannot read PE header of ${binary}: $($_.Exception.Message)"
        return $null
    }
    if ($machine -ne 0x8664) {
        Write-Warning ("bin/codex.exe is not x86_64 (PE machine 0x{0:X4})" -f $machine)
        return $null
    }

    $reported = Get-CodexVersion -Binary $binary
    if (-not $reported) {
        Write-Warning "cannot read codex.exe --version from $binary"
        return $null
    }

    $manifest = Read-PackageManifest -Path $packageJson
    $manifestVersion = Get-ManifestField -Manifest $manifest -Name "version"
    $manifestTarget = Get-ManifestField -Manifest $manifest -Name "target"

    if ($manifestVersion -and $manifestVersion -ne $reported) {
        Write-Warning "codex-package.json version $manifestVersion does not match codex.exe --version $reported"
        return $null
    }
    if ($manifestTarget -and $manifestTarget -ne $script:codexTarget) {
        Write-Warning "codex-package.json target $manifestTarget is not $($script:codexTarget)"
        return $null
    }
    if ($ExpectedVersion -and $reported -ne $ExpectedVersion) {
        Write-Warning "codex.exe --version $reported does not match requested $ExpectedVersion"
        return $null
    }
    if ($manifestVersion -and $ExpectedVersion -and $manifestVersion -ne $ExpectedVersion) {
        Write-Warning "codex-package.json version $manifestVersion does not match requested $ExpectedVersion"
        return $null
    }

    return $reported
}

$scriptDir = $PSScriptRoot
if (-not $scriptDir) {
    $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
}
$repoRoot = (Resolve-Path (Join-Path $scriptDir "../..")).Path

Assert-WindowsX64

$explicitVersion = $false
$requestedVersion = $Version
if (-not $requestedVersion -and $env:CODEX_VERSION) {
    $requestedVersion = $env:CODEX_VERSION
}
if ($requestedVersion) {
    $explicitVersion = $true
} else {
    $requestedVersion = $script:defaultCodexVersion
}
if (-not (Test-VersionFormat -Value $requestedVersion)) {
    Die "invalid Codex version: $requestedVersion"
}

$overrideRoot = $ReleaseRoot
if (-not $overrideRoot -and $env:CODEX_RELEASE_ROOT) {
    $overrideRoot = $env:CODEX_RELEASE_ROOT
}
$overrideBin = $Bin
if (-not $overrideBin -and $env:CODEX_BIN) {
    $overrideBin = $env:CODEX_BIN
}

if ($Validate) {
    if (-not $overrideRoot) {
        if ($overrideBin) {
            $overrideRoot = Resolve-ReleaseRootFromBin -Binary $overrideBin
        } else {
            Die "-Validate requires -ReleaseRoot or CODEX_RELEASE_ROOT"
        }
    }
    if (-not (Test-Path -LiteralPath $overrideRoot -PathType Container)) {
        Die "release root not found: $overrideRoot"
    }
    $expected = ""
    if ($explicitVersion) {
        $expected = $requestedVersion
    } else {
        $manifest = Read-PackageManifest -Path (Join-Path $overrideRoot "codex-package.json")
        $expected = Get-ManifestField -Manifest $manifest -Name "version"
    }
    $reported = Test-CodexLayout -Layout $overrideRoot -ExpectedVersion $expected
    if (-not $reported) {
        Die "invalid Codex release at $overrideRoot"
    }
    Write-Output $reported
    exit 0
}

# Reuse an explicitly supplied layout, but only after the same full validation.
if ($overrideRoot -or $overrideBin) {
    if (-not $overrideRoot) {
        $overrideRoot = Resolve-ReleaseRootFromBin -Binary $overrideBin
    }
    if (-not (Test-Path -LiteralPath $overrideRoot -PathType Container)) {
        Die "release root not found: $overrideRoot"
    }
    $expected = ""
    if ($explicitVersion) {
        $expected = $requestedVersion
    } else {
        $manifest = Read-PackageManifest -Path (Join-Path $overrideRoot "codex-package.json")
        $expected = Get-ManifestField -Manifest $manifest -Name "version"
    }
    $reported = Test-CodexLayout -Layout $overrideRoot -ExpectedVersion $expected
    if (-not $reported) {
        Die "invalid Codex release at $overrideRoot"
    }
    $resolvedRoot = (Resolve-Path -LiteralPath $overrideRoot).Path
    Write-Diagnostic "using verified Codex $reported from $resolvedRoot"
    Write-Output $resolvedRoot
    exit 0
}

if (-not $BuildRoot) {
    $BuildRoot = Join-Path $repoRoot ".build/codex"
}
$buildRoot = [IO.Path]::GetFullPath($BuildRoot)
New-Item -ItemType Directory -Path $buildRoot -Force | Out-Null
$releaseDir = Join-Path $buildRoot "$requestedVersion-$($script:codexTarget)"

if (Test-Path -LiteralPath $releaseDir -PathType Container) {
    $reported = Test-CodexLayout -Layout $releaseDir -ExpectedVersion $requestedVersion
    if ($reported) {
        Write-Diagnostic "reusing verified Codex $reported at $releaseDir"
        Write-Output $releaseDir
        exit 0
    }
    Write-Warning "cached release at $releaseDir failed verification; refetching"
    Remove-Item -LiteralPath $releaseDir -Recurse -Force
}

$script:tempDir = Join-Path $buildRoot (".download." + [System.IO.Path]::GetRandomFileName())
New-Item -ItemType Directory -Path $script:tempDir -Force | Out-Null

try {
    $manifestPath = Join-Path $script:tempDir $script:codexChecksumAsset
    $archivePath = Join-Path $script:tempDir $script:codexAsset
    $manifestUrl = "$($script:releasesBaseUrl)/releases/$requestedVersion/$($script:codexChecksumAsset)"
    $archiveUrl = "$($script:releasesBaseUrl)/releases/$requestedVersion/$($script:codexAsset)"
    $manifestFallback = "$($script:githubBaseUrl)/rust-v$requestedVersion/$($script:codexChecksumAsset)"
    $archiveFallback = "$($script:githubBaseUrl)/rust-v$requestedVersion/$($script:codexAsset)"

    Write-Diagnostic "downloading $($script:codexChecksumAsset) for Codex $requestedVersion"
    Get-RemoteFileWithFallback -Primary $manifestUrl -Fallback $manifestFallback -Destination $manifestPath
    $expectedDigest = Get-ManifestDigest -ManifestPath $manifestPath -Asset $script:codexAsset
    if (-not $expectedDigest) {
        Die "no SHA-256 entry for $($script:codexAsset) in $($script:codexChecksumAsset)"
    }

    Write-Diagnostic "downloading $($script:codexAsset) for Codex $requestedVersion"
    Get-RemoteFileWithFallback -Primary $archiveUrl -Fallback $archiveFallback -Destination $archivePath
    $actualDigest = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualDigest -ne $expectedDigest) {
        Die "SHA-256 mismatch for $($script:codexAsset) (expected $expectedDigest, got $actualDigest)"
    }
    Write-Diagnostic "SHA-256 verified for $($script:codexAsset)"

    $tar = Get-TarPath
    $script:stageDir = Join-Path $buildRoot (".staging.$requestedVersion." + [System.Guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $script:stageDir -Force | Out-Null
    & $tar -xzf $archivePath -C $script:stageDir
    if ($LASTEXITCODE -ne 0) {
        Die "tar.exe failed to extract $($script:codexAsset) (exit code $LASTEXITCODE)"
    }

    $reported = Test-CodexLayout -Layout $script:stageDir -ExpectedVersion $requestedVersion
    if (-not $reported) {
        Die "downloaded Codex $requestedVersion failed layout verification"
    }

    if (Test-Path -LiteralPath $releaseDir) {
        Remove-Item -LiteralPath $releaseDir -Recurse -Force
    }
    Move-Item -LiteralPath $script:stageDir -Destination $releaseDir
    $script:stageDir = $null
    Write-Diagnostic "staged verified Codex $reported at $releaseDir"
    Write-Output $releaseDir
} catch {
    Die $_.Exception.Message
} finally {
    Remove-Staging
}

exit 0
