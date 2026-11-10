param(
    [ValidateSet('Start', 'Stop', 'Status', 'Build')]
    [string]$Action = 'Status',
    [ValidateRange(1024, 65535)]
    [int]$Port = 18080,
    [switch]$NoAutomation
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$entry = Join-Path $projectRoot 'main.py'
$localDir = Join-Path $projectRoot 'local'
$stateFile = Join-Path $localDir 'web-process.json'

function Get-OwnedProcess {
    if (!(Test-Path -LiteralPath $stateFile)) { return $null }
    $script:state = Get-Content -LiteralPath $stateFile -Raw -Encoding UTF8 | ConvertFrom-Json
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $($state.ProcessId)"
    if (!$process) { return $null }
    if ($state.ProjectRoot -ne $projectRoot -or $process.ExecutablePath -ne $python -or
        [string]$process.CreationDate.ToUniversalTime().Ticks -ne $state.StartTicks -or
        $process.CommandLine -ne $state.CommandLine -or
        $process.CommandLine.IndexOf($entry, [StringComparison]::OrdinalIgnoreCase) -lt 0 -or
        $process.CommandLine -notmatch '--serve-only --no-notify --no-run-immediately --host 127\.0\.0\.1 --port \d+') {
        throw 'Saved PID identity does not match this project. Refusing to stop or replace it.'
    }
    return $process
}

function Invoke-ProjectEnvironment([scriptblock]$Command) {
    # Read key names only; the application's dotenv loader reads the real .env.
    $keys = @(Get-Content -LiteralPath (Join-Path $projectRoot '.env.example') |
        ForEach-Object {
            if ($_ -match '^\s*(?:#\s*)?(?:export\s+)?([A-Z_][A-Z0-9_]*)\s*=') { $Matches[1] }
        })
    $keys += @(Get-ChildItem Env: | Where-Object {
        $_.Name -match '^(LLM_|DSA_|OPENAI_|ANTHROPIC_|GEMINI_|DEEPSEEK_)|_API_KEYS?$|_TOKEN$'
    } | ForEach-Object { $_.Name })
    $settings = @{
        ENV_FILE = (Join-Path $projectRoot '.env')
        PYTHONUTF8 = '1'; PYTHONNOUSERSITE = '1'; WEBUI_AUTO_BUILD = 'false'
        DSA_RESEARCH_AUTOMATION_SUPPRESS_START = $(if ($NoAutomation) { 'true' } else { 'false' })
        LOG_DIR = (Join-Path $localDir 'logs')
        XDG_CACHE_HOME = (Join-Path $localDir 'cache')
        HF_HOME = (Join-Path $localDir 'cache\huggingface')
        HUGGINGFACE_HUB_CACHE = (Join-Path $localDir 'cache\huggingface\hub')
        TRANSFORMERS_CACHE = (Join-Path $localDir 'cache\transformers')
        TORCH_HOME = (Join-Path $localDir 'cache\torch')
        MPLCONFIGDIR = (Join-Path $localDir 'cache\matplotlib')
        NUMBA_CACHE_DIR = (Join-Path $localDir 'cache\numba')
        NPM_CONFIG_CACHE = (Join-Path $localDir 'cache\npm')
    }
    $keys += @('PYTHONHOME', 'PYTHONPATH', 'VIRTUAL_ENV', 'GITHUB_ACTIONS') + @($settings.Keys)
    $saved = @{}
    try {
        foreach ($key in ($keys | Select-Object -Unique)) {
            $saved[$key] = [Environment]::GetEnvironmentVariable($key, 'Process')
            # Typed null removes the key; an empty string overrides dotenv defaults.
            [Environment]::SetEnvironmentVariable($key, [NullString]::Value, 'Process')
        }
        foreach ($key in $settings.Keys) {
            [Environment]::SetEnvironmentVariable($key, $settings[$key], 'Process')
        }
        New-Item -ItemType Directory -Path $settings.LOG_DIR, $settings.XDG_CACHE_HOME -Force | Out-Null
        & $Command
    } finally {
        foreach ($key in $saved.Keys) {
            if ($null -eq $saved[$key]) {
                [Environment]::SetEnvironmentVariable($key, [NullString]::Value, 'Process')
            } else {
                [Environment]::SetEnvironmentVariable($key, $saved[$key], 'Process')
            }
        }
    }
}

if ($Action -eq 'Build') {
    $frontend = Join-Path $projectRoot 'apps\dsa-web'
    if (!(Test-Path -LiteralPath (Join-Path $frontend 'node_modules\.bin\vite.cmd'))) {
        throw 'Frontend dependencies are missing. Install them in apps/dsa-web first.'
    }
    Invoke-ProjectEnvironment {
        Push-Location $frontend
        try {
            & npm.cmd run build
            if ($LASTEXITCODE -ne 0) { throw "Frontend build failed (exit $LASTEXITCODE)." }
        } finally { Pop-Location }
    }
    return
}

$owned = Get-OwnedProcess
if ($Action -eq 'Status') {
    if ($owned) { Write-Output "Running: http://127.0.0.1:$($state.Port) (PID $($owned.ProcessId))" }
    else { Write-Output 'Not running (no matching saved process).' }
    return
}

if ($Action -eq 'Stop') {
    if (!$owned) { Write-Output 'Not running; nothing stopped.'; return }
    $all = @(Get-CimInstance Win32_Process)
    $tree = @($owned)
    for ($i = 0; $i -lt $tree.Count; $i++) {
        $parent = $tree[$i]
        $tree += @($all | Where-Object {
            $_.ParentProcessId -eq $parent.ProcessId -and $_.CreationDate -ge $parent.CreationDate
        })
    }
    # Verify every captured identity before stopping anything, then recheck each PID.
    foreach ($item in $tree) {
        $current = Get-CimInstance Win32_Process -Filter "ProcessId = $($item.ProcessId)"
        if ($current -and ($current.CreationDate -ne $item.CreationDate -or
            !$current.CommandLine -or $current.CommandLine -ne $item.CommandLine)) {
            throw 'Process tree identity changed. Refusing to stop it.'
        }
    }
    for ($i = $tree.Count - 1; $i -ge 0; $i--) {
        $item = $tree[$i]
        $current = Get-CimInstance Win32_Process -Filter "ProcessId = $($item.ProcessId)"
        if ($current) {
            if ($current.CreationDate -ne $item.CreationDate -or $current.CommandLine -ne $item.CommandLine) {
                throw 'PID was reused. Refusing to stop it.'
            }
            Stop-Process -Id $current.ProcessId -ErrorAction Stop
        }
    }
    Remove-Item -LiteralPath $stateFile
    Write-Output 'Stopped this project process tree. Logs and data were kept.'
    return
}

if ($owned) { throw "Already running on port $($state.Port). Use Stop before starting again." }
foreach ($required in @($python, (Join-Path $projectRoot '.env'), (Join-Path $projectRoot 'static\index.html'))) {
    if (!(Test-Path -LiteralPath $required)) { throw "Missing required file: $required. Complete setup/Build first." }
}
$probe = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $Port)
try { $probe.Start() } catch { throw "Port $Port is unavailable. Choose another -Port; no process was stopped." }
finally { $probe.Stop() }

Invoke-ProjectEnvironment {
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
    $stdout = Join-Path $localDir "logs\web-$stamp.out.log"
    $stderr = Join-Path $localDir "logs\web-$stamp.err.log"
    $process = Start-Process -FilePath $python -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru `
        -ArgumentList "`"$entry`" --serve-only --no-notify --no-run-immediately --host 127.0.0.1 --port $Port" `
        -RedirectStandardOutput $stdout -RedirectStandardError $stderr
    $identity = Get-CimInstance Win32_Process -Filter "ProcessId = $($process.Id)"
    if (!$identity) { throw "Backend exited before its PID could be saved. See $stderr" }
    @{ ProjectRoot = $projectRoot; ProcessId = $process.Id; Port = $Port
        StartTicks = [string]$identity.CreationDate.ToUniversalTime().Ticks
        CommandLine = $identity.CommandLine } | ConvertTo-Json | Set-Content -LiteralPath $stateFile -Encoding UTF8
    Write-Output "Started backend process: http://127.0.0.1:$Port (PID $($process.Id)). Startup may take a moment."
    Write-Output "Logs: $stdout and $stderr"
}
