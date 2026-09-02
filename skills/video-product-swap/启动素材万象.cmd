@echo off
setlocal
cd /d "%~dp0"

where pyw >nul 2>nul
if %errorlevel%==0 (
  start "" /D "%~dp0" pyw -3 "%~dp0scripts\launch_gui.pyw"
  exit /b 0
)

where pythonw >nul 2>nul
if %errorlevel%==0 (
  start "" /D "%~dp0" pythonw "%~dp0scripts\launch_gui.pyw"
  exit /b 0
)

echo 未找到 Python。请先安装 Python 3，然后重新双击此文件。
pause
