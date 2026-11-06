@echo off
rem Runs the dashboard in this window. Closing the window, or Ctrl+C, stops it and saves its readings.
title Solar monitor
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo No .venv here yet. Create it and run: pip install -e .
    pause
    exit /b 1
)

.venv\Scripts\python.exe -m solar_monitor.main

rem Leave the window open after a failure, such as port 8000 already being in use.
if errorlevel 1 pause
