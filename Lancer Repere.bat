@echo off
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel% equ 0 (
  py -3 radar.py --open %*
) else (
  python radar.py --open %*
)
if errorlevel 1 pause
