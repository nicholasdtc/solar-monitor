@echo off
rem Stops every running copy of the dashboard, including one left running in the background.
title Stop solar monitor
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo No .venv here yet. Create it and run: pip install -e .
    pause
    exit /b 1
)

.venv\Scripts\python.exe -m solar_monitor.stop

rem Leave the window open so the result can be read.
pause
