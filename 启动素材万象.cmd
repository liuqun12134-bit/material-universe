@echo off
setlocal
cd /d "%~dp0skills\video-product-swap"

where pyw >nul 2>nul
if %errorlevel%==0 (
  start "" /D "%~dp0skills\video-product-swap" pyw -3 "%~dp0skills\video-product-swap\scripts\launch_gui.pyw"
  exit /b 0
)

where pythonw >nul 2>nul
if %errorlevel%==0 (
  start "" /D "%~dp0skills\video-product-swap" pythonw "%~dp0skills\video-product-swap\scripts\launch_gui.pyw"
  exit /b 0
)

echo 未找到 Python。请先安装 Python 3，然后重新双击此文件。
pause
