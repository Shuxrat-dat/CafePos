@echo off
rem CafePOS print agent: autostart when the computer is turned on. Run as administrator.
rem Safe to run again: replaces the task with the same settings.
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

net session >nul 2>&1
if errorlevel 1 (
  echo Run install_print_agent.bat as administrator: right click - Run as administrator.
  pause
  exit /b 1
)

where py >nul 2>&1
if errorlevel 1 (
  echo Python not found. Install Python 3.10 or newer from python.org and run this file again.
  pause
  exit /b 1
)

if not exist cafepos_print_agent.ini (
  echo cafepos_print_agent.ini not found. Download the agent again in Settings - Printers.
  pause
  exit /b 1
)

for /f "delims=" %%i in ('py -3 -c "import sys; print(sys.executable)"') do set PYEXE=%%i

echo [1/2] pywin32 for USB printers
"%PYEXE%" -m pip install --disable-pip-version-check -q pywin32
if errorlevel 1 echo pywin32 was not installed: network (LAN) printers still work, USB printers will not.

echo [2/2] Autostart task "CafePOS print agent"
powershell -NoProfile -ExecutionPolicy Bypass -File install_print_agent.ps1 -Root "%CD%" -Python "%PYEXE%"
if errorlevel 1 (
  echo Could not register the task.
  pause
  exit /b 1
)

echo.
echo Done. The print agent is running and will start by itself every time the computer is turned on.
echo Log: %CD%\cafepos_print_agent.log
echo.
pause
