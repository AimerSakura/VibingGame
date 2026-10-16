param(
    [string]$OutputDir = ".build/VibeGodot-Windows-x86_64",
    [string]$PythonExe = "",
    [switch]$SkipBuild,
    [switch]$SkipSidecarBuild,
    [switch]$Zip
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path
$output = [IO.Path]::GetFullPath((Join-Path $repoRoot $OutputDir))
$buildRoot = [IO.Path]::GetFullPath((Join-Path $repoRoot ".build"))
$engine = Join-Path $repoRoot "bin/godot.windows.editor.x86_64.exe"

if (-not $PythonExe) {
    $PythonExe = if ($env:VIBE_BUILD_PYTHON) { $env:VIBE_BUILD_PYTHON } else { (Get-Command python -ErrorAction Stop).Source }
}
$PythonExe = (Resolve-Path -LiteralPath $PythonExe).Path

if (-not $SkipBuild) {
    & $PythonExe -m SCons platform=windows target=editor production=yes debug_symbols=no d3d12=no
    if ($LASTEXITCODE -ne 0) { throw "Godot editor build failed." }
}
if (-not (Test-Path -LiteralPath $engine)) {
    $developerEngine = Join-Path $repoRoot "bin/godot.windows.editor.dev.x86_64.exe"
    if ($SkipBuild -and (Test-Path -LiteralPath $developerEngine)) {
        $engine = $developerEngine
    } else {
        throw "Editor binary not found: $engine"
    }
}

if (-not $SkipSidecarBuild) {
    & (Join-Path $PSScriptRoot "build_sidecar.ps1") -PythonExe $PythonExe
    if ($LASTEXITCODE -ne 0) { throw "Agent sidecar build failed." }
}
$sidecar = Join-Path $repoRoot ".build/sidecar-dist/godotvibe-agent"
if (-not (Test-Path -LiteralPath (Join-Path $sidecar "godotvibe-agent.exe"))) {
    throw "Agent sidecar not found. Run misc/vibe_agent/build_sidecar.ps1 first."
}

# Fetch, verify and stage the official Codex Windows x86_64 release before
# touching the output directory. CODEX_VERSION, CODEX_RELEASE_ROOT and CODEX_BIN
# pass through for development overrides; fetch_codex_windows.ps1 still enforces
# the SHA-256 manifest, the x86_64 PE layout and the requested version. No token,
# model or working directory is ever passed on the command line.
$fetchCodex = Join-Path $PSScriptRoot "fetch_codex_windows.ps1"
$codexLayout = & $fetchCodex
if ($LASTEXITCODE -ne 0 -or -not $codexLayout) {
    throw "Codex fetch failed."
}
$codexLayout = [IO.Path]::GetFullPath([string]$codexLayout)
$codexVersion = & $fetchCodex -Validate -ReleaseRoot $codexLayout
if ($LASTEXITCODE -ne 0 -or -not $codexVersion) {
    throw "Codex release validation failed."
}
$codexVersion = [string]$codexVersion

if (Test-Path -LiteralPath $output) {
    if (-not $output.StartsWith($buildRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw "For safety, OutputDir must be under $buildRoot"
    }
    Remove-Item -LiteralPath $output -Recurse -Force
}

$sidecarTarget = Join-Path $output "vibe_agent/godotvibe-agent"
$codexTarget = Join-Path $output "vibe_agent/codex"
$licenses = Join-Path $output "licenses/python-packages"
New-Item -ItemType Directory -Path $sidecarTarget, $codexTarget, $licenses -Force | Out-Null
Copy-Item -LiteralPath $engine -Destination (Join-Path $output "VibeGodot.exe")
$console = $engine.Replace(".exe", ".console.exe")
if (Test-Path -LiteralPath $console) {
    Copy-Item -LiteralPath $console -Destination (Join-Path $output "VibeGodot.console.exe")
}
Copy-Item -Path (Join-Path $sidecar "*") -Destination $sidecarTarget -Recurse -Force
Get-ChildItem -LiteralPath $codexLayout -Force | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $codexTarget -Recurse -Force
}

# Re-verify the copied layout so a broken bundle fails packaging instead of the
# sidecar at runtime.
$copiedVersion = & $fetchCodex -Validate -ReleaseRoot $codexTarget -Version $codexVersion
if ($LASTEXITCODE -ne 0 -or -not $copiedVersion) {
    throw "Copied Codex release failed validation."
}

Copy-Item -LiteralPath (Join-Path $repoRoot "LICENSE.txt") -Destination (Join-Path $output "GODOT_LICENSE.txt")
$pythonLicense = Join-Path (Split-Path $PythonExe) "LICENSE.txt"
if (Test-Path -LiteralPath $pythonLicense) {
    Copy-Item -LiteralPath $pythonLicense -Destination (Join-Path $output "PYTHON_LICENSE.txt")
}
$libraryRoot = Join-Path $repoRoot ".build/pythonlibs"
Get-ChildItem -Path $libraryRoot -Directory -Filter "*.dist-info" | ForEach-Object {
    $licenseDir = Join-Path $_.FullName "licenses"
    if (Test-Path -LiteralPath $licenseDir) {
        $target = Join-Path $licenses $_.Name
        New-Item -ItemType Directory -Path $target -Force | Out-Null
        Copy-Item -Path (Join-Path $licenseDir "*") -Destination $target -Recurse -Force
    }
}
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "README.zh-CN.md") -Destination (Join-Path $output "使用说明.md")
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "CAPABILITIES.md") -Destination (Join-Path $output "能力对应表.md")

if ($Zip) {
    $archive = "$output.zip"
    if (Test-Path -LiteralPath $archive) { Remove-Item -LiteralPath $archive -Force }
    Compress-Archive -LiteralPath $output -DestinationPath $archive -CompressionLevel Optimal
    Write-Host "Package created: $archive"
} else {
    Write-Host "Package created: $output"
}
