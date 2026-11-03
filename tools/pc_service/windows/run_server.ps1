# Donggu bulky-waste PC server - background runner for Windows (Task Scheduler).
# Keeps "python pc\pc_tool.py serve" running and restarts it 10 s after it exits.
# Logs: <DG_STATE>\logs  (default: <repo>\pc\state\logs)
#   service.log                 start/exit history of this runner
#   service-<time>.out/.err.log console output of each server run (newest 40 kept)
#   the server's own log files are written to the same folder by the server.
# The DB key must already be in Windows Credential Manager (run "python pc\pc_tool.py init"
# once, interactively, as the same Windows user). This runner never prompts.
# Kept ASCII-only on purpose: Windows PowerShell 5.1 misreads UTF-8 files without BOM.
param(
    [string]$Repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path,
    [int]$Port = 8765,
    [int]$RestartDelaySec = 10,
    [int]$KeepRunLogs = 40
)
$ErrorActionPreference = 'Stop'

$State = if ($env:DG_STATE) { $env:DG_STATE } else { Join-Path $Repo 'pc\state' }
$LogDir = Join-Path $State 'logs'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$RunnerLog = Join-Path $LogDir 'service.log'

$Py = Join-Path $Repo '.venv\Scripts\python.exe'
if (-not (Test-Path $Py)) { $Py = (Get-Command python -ErrorAction Stop).Source }
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

function Write-RunnerLog([string]$msg) {
    Add-Content -Path $RunnerLog -Encoding UTF8 -Value ("{0} {1}" -f (Get-Date -Format 's'), $msg)
}

Write-RunnerLog "runner started (python=$Py, repo=$Repo, port=$Port)"
while ($true) {
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $out = Join-Path $LogDir "service-$stamp.out.log"
    $err = Join-Path $LogDir "service-$stamp.err.log"
    Write-RunnerLog "server start"
    $p = Start-Process -FilePath $Py -ArgumentList @('pc\pc_tool.py', 'serve', '--port', "$Port") `
        -WorkingDirectory $Repo -NoNewWindow -PassThru -Wait `
        -RedirectStandardOutput $out -RedirectStandardError $err
    Write-RunnerLog ("server exited with code {0}; restart in {1} s" -f $p.ExitCode, $RestartDelaySec)
    Get-ChildItem -Path $LogDir -Filter 'service-*.log' |
        Sort-Object LastWriteTime -Descending | Select-Object -Skip ($KeepRunLogs * 2) |
        Remove-Item -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds $RestartDelaySec
}
