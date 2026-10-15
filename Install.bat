@echo off
title Glance setup
cd /d "%~dp0"
set PY=
where py >nul 2>nul && set PY=py
if not defined PY where python >nul 2>nul && set PY=python
if not defined PY (
  echo Python was not found.
  echo Install it from https://www.python.org/downloads/  ^(tick "Add python.exe to PATH"^), then run this again.
  pause
  exit /b 1
)
echo Installing PySide6 and psutil...
%PY% -m pip install --user --upgrade -r requirements.txt
if errorlevel 1 (
  echo.
  echo Install failed - see the messages above.
  pause
  exit /b 1
)
echo.
echo Done. Double-click "Start Glance.bat" to run it.
timeout /t 15
