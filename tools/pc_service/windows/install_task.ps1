# Register (or re-register) the Donggu PC server as a Task Scheduler task for the current user.
# Run in PowerShell as the operator account (no admin needed):
#   powershell -ExecutionPolicy Bypass -File tools\pc_service\windows\install_task.ps1
#   powershell -ExecutionPolicy Bypass -File tools\pc_service\windows\install_task.ps1 -Uninstall
# Before installing: run "python pc\pc_tool.py init" once interactively so the DB key is in Credential Manager.
# Kept ASCII-only on purpose (Windows PowerShell 5.1 + UTF-8 without BOM).
param(
    [string]$TaskName = 'DongguWastePC',
    [string]$Repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path,
    [string]$User = "$env:USERDOMAIN\$env:USERNAME",
    [switch]$Uninstall,
    [switch]$NoStart
)
$ErrorActionPreference = 'Stop'

if ($Uninstall) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed task $TaskName. Logs and the DB were not touched."
    return
}

if (-not (Test-Path (Join-Path $Repo 'pc\pc_tool.py'))) { throw "pc\pc_tool.py not found under $Repo" }
$xml = Get-Content -Raw -Encoding UTF8 (Join-Path $PSScriptRoot 'donggu-pc-task.xml')
$esc = { param($s) [System.Security.SecurityElement]::Escape($s) }
$xml = $xml.Replace('__REPO__', (& $esc $Repo)).Replace('__USER__', (& $esc $User)).Replace('\DongguWastePC', "\$TaskName")

Register-ScheduledTask -TaskName $TaskName -Xml $xml -Force | Out-Null
Write-Host "Registered task $TaskName for $User (starts at logon; repo $Repo)."
if (-not $NoStart) {
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 5
    try {
        $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 -Uri 'http://127.0.0.1:8765/'
        Write-Host "Server is answering: HTTP $($r.StatusCode) on http://127.0.0.1:8765/"
    } catch {
        Write-Warning "Server not answering yet. Check the logs in pc\state\logs (service.log, service-*.err.log)."
    }
}
