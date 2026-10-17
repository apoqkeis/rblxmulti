@echo off
title Roblox Headless Multi-Instance Launcher
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found on PATH. Install it from https://www.python.org/downloads/
    echo (make sure "Add python.exe to PATH" is checked during installation)
    pause
    exit /b 1
)
python roblox_launcher.py %*
if errorlevel 1 pause