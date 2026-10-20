@echo off
rem CafePOS print agent: run in this window (to check that printing works).
rem For autostart when the computer is turned on, run install_print_agent.bat as administrator.
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
where py >nul 2>&1
if errorlevel 1 (
  echo Python not found. Install Python 3.10 or newer from python.org.
  pause
  exit /b 1
)
py -3 cafepos_print_agent.py %*
pause
