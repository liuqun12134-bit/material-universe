@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\packaging\restore_release.ps1"
echo.
pause
