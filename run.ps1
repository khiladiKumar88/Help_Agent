#!/usr/bin/env pwsh
<#
.SYNOPSIS
    PaperMind task runner for Windows / PowerShell — the Makefile without `make`.

.DESCRIPTION
    Every path is derived from $PSScriptRoot and passed with -LiteralPath / as a single
    argument, so folder names containing spaces (or '(', ')', '[' ...) are safe.

    PaperMind is paper-only. None of these commands can place a real order.

.PARAMETER Command
    install        backend deps (uv sync) + frontend deps (npm install)
    dev            backend (live public crypto data via ccxt) + frontend, together
    dev-sim        same, but with the offline SIMULATED feed (no network needed)
    test           backend pytest (with coverage) + frontend vitest
    check          everything CI would run: lint + typecheck + tests
    lint           ruff check + ruff format --check
    typecheck      mypy --strict + tsc
    test-backend   backend tests only
    test-frontend  frontend tests only
    help           this list

.PARAMETER DryRun
    Print the commands that would run, with their working directory, and change nothing.

.EXAMPLE
    .\run.ps1 install
.EXAMPLE
    .\run.ps1 dev          # UI http://127.0.0.1:5173 · API http://127.0.0.1:8000/docs
.EXAMPLE
    .\run.ps1 check
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('install', 'dev', 'dev-sim', 'test', 'check', 'lint', 'typecheck',
        'test-backend', 'test-frontend', 'help')]
    [string]$Command = 'help',

    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Root = $PSScriptRoot
$BackendDir = Join-Path $Root 'backend'
$FrontendDir = Join-Path $Root 'frontend'

$ApiUrl = 'http://127.0.0.1:8000/docs'
$UiUrl = 'http://127.0.0.1:5173'

# resolved lazily, once per run (Set-StrictMode requires them to exist before being read)
$script:UvPath = $null
$script:NpmPath = $null

# --------------------------------------------------------------------------- tools

function Resolve-Tool {
    <#  Find an executable on PATH, falling back to the usual per-user install locations.
        Returns the full path so it can be invoked with `&` regardless of spaces. #>
    param(
        [Parameter(Mandatory)][string]$Name,
        [string[]]$Fallbacks = @(),
        [string]$HowToInstall = ''
    )
    $found = Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($found) { return $found.Source }
    foreach ($candidate in $Fallbacks) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) { return (Resolve-Path -LiteralPath $candidate).Path }
    }
    $msg = "'$Name' was not found on PATH."
    if ($HowToInstall) { $msg += " $HowToInstall" }
    throw $msg
}

function Get-Uv {
    if ($script:UvPath) { return $script:UvPath }
    # uv's default per-user location is ~\.local\bin (this machine: C:\Users\<you>\.local\bin)
    $script:UvPath = Resolve-Tool -Name 'uv' -HowToInstall 'Install it from https://docs.astral.sh/uv/ or add it to PATH.' -Fallbacks @(
        (Join-Path $HOME '.local\bin\uv.exe'),
        (Join-Path $env:USERPROFILE '.local\bin\uv.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\uv\uv.exe')
    )
    return $script:UvPath
}

function Get-Npm {
    if ($script:NpmPath) { return $script:NpmPath }
    # `npm` on Windows is npm.cmd; Get-Command -CommandType Application resolves it
    $script:NpmPath = Resolve-Tool -Name 'npm' -HowToInstall 'Install Node.js 20+ from https://nodejs.org/.' -Fallbacks @(
        (Join-Path $env:ProgramFiles 'nodejs\npm.cmd'),
        (Join-Path $env:APPDATA 'npm\npm.cmd')
    )
    return $script:NpmPath
}

# --------------------------------------------------------------------------- running

function Show-Step {
    param([string]$Dir, [string]$Exe, [string[]]$Arguments)
    $rel = if ($Dir.StartsWith($Root, [StringComparison]::OrdinalIgnoreCase)) {
        $Dir.Substring($Root.Length).TrimStart('\', '/')
    } else { $Dir }
    if (-not $rel) { $rel = '.' }
    $shown = @($Exe) + $Arguments | ForEach-Object { if ($_ -match '[ ()\[\]]') { """$_""" } else { $_ } }
    Write-Host "==> [$rel] $($shown -join ' ')" -ForegroundColor Cyan
}

function Invoke-Step {
    <# Run one command to completion in $Dir and fail the script on a non-zero exit code. #>
    param(
        [Parameter(Mandatory)][string]$Dir,
        [Parameter(Mandatory)][string]$Exe,
        [string[]]$Arguments = @()
    )
    Show-Step -Dir $Dir -Exe $Exe -Arguments $Arguments
    if ($DryRun) { return }
    Push-Location -LiteralPath $Dir
    try {
        & $Exe @Arguments
        if ($LASTEXITCODE -ne 0) {
            throw "'$(Split-Path -Leaf $Exe) $($Arguments -join ' ')' failed with exit code $LASTEXITCODE."
        }
    } finally {
        Pop-Location
    }
}

function Stop-Tree {
    <# Kill a process and its children — uvicorn --reload and vite both spawn workers. #>
    param([int]$ProcessId)
    if (-not (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) { return }
    & taskkill.exe /PID $ProcessId /T /F *>$null
}

function Invoke-Together {
    <# Start several long-running servers in this console and keep them alive as a group:
       the first one to exit (or Ctrl+C) takes the others down with it. #>
    param([Parameter(Mandatory)][hashtable[]]$Steps)

    foreach ($s in $Steps) { Show-Step -Dir $s.Dir -Exe $s.Exe -Arguments $s.Arguments }
    if ($DryRun) { return }

    $procs = @()
    try {
        foreach ($s in $Steps) {
            $procs += Start-Process -FilePath $s.Exe -ArgumentList $s.Arguments `
                -WorkingDirectory $s.Dir -NoNewWindow -PassThru
        }
        Write-Host ''
        Write-Host "    UI  $UiUrl" -ForegroundColor Green
        Write-Host "    API $ApiUrl" -ForegroundColor Green
        Write-Host '    Ctrl+C stops both.' -ForegroundColor DarkGray
        Write-Host ''
        while (-not ($procs | Where-Object { $_.HasExited })) { Start-Sleep -Milliseconds 400 }
    } finally {
        foreach ($p in $procs) { Stop-Tree -ProcessId $p.Id }
    }
}

function Use-Env {
    <# Run $Body with extra environment variables set, restoring them afterwards.
       Child processes inherit them, which is how dev-sim reaches uvicorn. #>
    param([Parameter(Mandatory)][hashtable]$Variables, [Parameter(Mandatory)][scriptblock]$Body)
    $saved = @{}
    foreach ($k in $Variables.Keys) { $saved[$k] = [Environment]::GetEnvironmentVariable($k) }
    try {
        foreach ($k in $Variables.Keys) {
            Write-Host "==> set $k=$($Variables[$k])" -ForegroundColor DarkCyan
            Set-Item -LiteralPath "Env:$k" -Value $Variables[$k]
        }
        & $Body
    } finally {
        foreach ($k in $Variables.Keys) {
            if ($null -eq $saved[$k]) { Remove-Item -LiteralPath "Env:$k" -ErrorAction SilentlyContinue }
            else { Set-Item -LiteralPath "Env:$k" -Value $saved[$k] }
        }
    }
}

# --------------------------------------------------------------------------- steps

function Step-Backend {
    @{ Dir = $BackendDir; Exe = (Get-Uv); Arguments = @(
            'run', 'uvicorn', 'papermind.main:app', '--host', '127.0.0.1', '--port', '8000', '--reload') }
}
function Step-Frontend {
    @{ Dir = $FrontendDir; Exe = (Get-Npm); Arguments = @('run', 'dev') }
}

function Invoke-Install {
    Invoke-Step -Dir $BackendDir -Exe (Get-Uv) -Arguments @('sync')
    Invoke-Step -Dir $FrontendDir -Exe (Get-Npm) -Arguments @('install')
}

function Invoke-TestBackend {
    Invoke-Step -Dir $BackendDir -Exe (Get-Uv) -Arguments @(
        'run', 'pytest', '--cov=papermind', '--cov-report=term-missing')
}
function Invoke-TestFrontend {
    Invoke-Step -Dir $FrontendDir -Exe (Get-Npm) -Arguments @('test')
}

function Invoke-Lint {
    Invoke-Step -Dir $BackendDir -Exe (Get-Uv) -Arguments @('run', 'ruff', 'check', '.')
    Invoke-Step -Dir $BackendDir -Exe (Get-Uv) -Arguments @('run', 'ruff', 'format', '--check', '.')
}

function Invoke-Typecheck {
    Invoke-Step -Dir $BackendDir -Exe (Get-Uv) -Arguments @('run', 'mypy', 'papermind')
    Invoke-Step -Dir $FrontendDir -Exe (Get-Npm) -Arguments @('run', 'typecheck')
}

function Show-Help {
    Write-Host 'PaperMind — paper trading only. Usage: .\run.ps1 <command> [-DryRun]' -ForegroundColor White
    Write-Host ''
    $rows = [ordered]@{
        'install'       = 'backend deps (uv sync) + frontend deps (npm install)'
        'dev'           = 'backend + frontend together, live public crypto data (needs internet)'
        'dev-sim'       = 'backend + frontend together, offline SIMULATED feed'
        'test'          = 'backend pytest (coverage) + frontend vitest'
        'check'         = 'everything CI would run: lint + typecheck + test'
        'lint'          = 'ruff check + ruff format --check'
        'typecheck'     = 'mypy --strict (backend) + tsc (frontend)'
        'test-backend'  = 'backend tests only'
        'test-frontend' = 'frontend tests only'
        'help'          = 'this list'
    }
    foreach ($k in $rows.Keys) { Write-Host ('  {0,-14} {1}' -f $k, $rows[$k]) }
    Write-Host ''
    Write-Host "  UI $UiUrl · API $ApiUrl" -ForegroundColor DarkGray
}

# --------------------------------------------------------------------------- dispatch

switch ($Command) {
    'install' { Invoke-Install }
    'dev' { Invoke-Together -Steps @((Step-Backend), (Step-Frontend)) }
    'dev-sim' {
        Use-Env -Variables @{
            PAPERMIND_CRYPTO_PROVIDER = 'simulated'
            PAPERMIND_DB_URL          = 'sqlite:///papermind-sim.db'
        } -Body { Invoke-Together -Steps @((Step-Backend), (Step-Frontend)) }
    }
    'test' { Invoke-TestBackend; Invoke-TestFrontend }
    'lint' { Invoke-Lint }
    'typecheck' { Invoke-Typecheck }
    'test-backend' { Invoke-TestBackend }
    'test-frontend' { Invoke-TestFrontend }
    'check' { Invoke-Lint; Invoke-Typecheck; Invoke-TestBackend; Invoke-TestFrontend }
    default { Show-Help }
}
