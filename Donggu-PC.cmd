@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\start_pc.ps1" %*
set "DG_LAUNCH_EXIT=%ERRORLEVEL%"
if not "%DG_LAUNCH_EXIT%"=="0" pause
exit /b %DG_LAUNCH_EXIT%
