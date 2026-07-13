[CmdletBinding()]
param(
    [string]$ProjectRoot = (Get-Location).Path,
    [string]$InstallRoot = (Join-Path $HOME ".joiny-mnemonic\runtime"),
    [string]$SourceRoot,
    [string]$Repository = "https://github.com/Barmagloth/Joiny-Mnemonic.git",
    [string]$Python = "python",
    [ValidateSet("project", "global")]
    [string]$Scope = "project",
    [string[]]$Agent = @(),
    [string[]]$Plugin = @(),
    [switch]$AllPlugins,
    [switch]$WithMcp,
    [switch]$WithoutHooks,
    [switch]$Yes,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$InstallRoot = [IO.Path]::GetFullPath($InstallRoot)
$ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot)

if (-not $SourceRoot -and (Test-Path -LiteralPath (Join-Path $PSScriptRoot "pyproject.toml"))) {
    $SourceRoot = $PSScriptRoot
}

if (-not $SourceRoot) {
    $SourceRoot = Join-Path $InstallRoot "source"
    if (Test-Path -LiteralPath (Join-Path $SourceRoot ".git")) {
        & git -C $SourceRoot pull --ff-only
        if ($LASTEXITCODE -ne 0) { throw "Failed to update Joiny-Mnemonic source" }
    } elseif (Test-Path -LiteralPath $SourceRoot) {
        throw "Source path exists but is not a Git checkout: $SourceRoot"
    } else {
        New-Item -ItemType Directory -Path $InstallRoot -Force | Out-Null
        & git clone --depth 1 $Repository $SourceRoot
        if ($LASTEXITCODE -ne 0) { throw "Failed to clone Joiny-Mnemonic" }
    }
}
$SourceRoot = [IO.Path]::GetFullPath($SourceRoot)
if (-not (Test-Path -LiteralPath (Join-Path $SourceRoot "pyproject.toml"))) {
    throw "Joiny-Mnemonic source is missing pyproject.toml: $SourceRoot"
}

$Venv = Join-Path $InstallRoot "venv"
$VenvPython = Join-Path $Venv "Scripts\python.exe"
if (-not (Test-Path -LiteralPath $VenvPython)) {
    New-Item -ItemType Directory -Path $InstallRoot -Force | Out-Null
    & $Python -m venv $Venv
    if ($LASTEXITCODE -ne 0) { throw "Failed to create Python virtual environment" }
}
& $VenvPython -m pip install $SourceRoot
if ($LASTEXITCODE -ne 0) { throw "Failed to install Joiny-Mnemonic core" }

$SetupArgs = @(
    "-m", "joiny_mnemonic",
    "--project-root", $ProjectRoot,
    "setup",
    "--scope", $Scope,
    "--source-root", $SourceRoot
)
foreach ($Value in $Agent) { $SetupArgs += @("--agent", $Value) }
foreach ($Value in $Plugin) { $SetupArgs += @("--plugin", $Value) }
if ($AllPlugins) { $SetupArgs += "--all-plugins" }
if ($WithMcp) { $SetupArgs += "--with-mcp" }
if ($WithoutHooks) { $SetupArgs += "--without-hooks" }
if ($Yes) { $SetupArgs += "--yes" }
if ($DryRun) { $SetupArgs += "--dry-run" }

& $VenvPython @SetupArgs
if ($LASTEXITCODE -ne 0) { throw "Joiny-Mnemonic setup failed" }

Write-Host "Joiny-Mnemonic runtime: $VenvPython"
