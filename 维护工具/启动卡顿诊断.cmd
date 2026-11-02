@echo off
start "" /b "%~dp0..\.build\venv\Scripts\pythonw.exe" "%~dp0..\packaging\diagnose_gui_hang.py"
