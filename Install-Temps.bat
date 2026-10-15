@echo off
title Glance - temperature setup
cd /d "%~dp0"
set PY=
where py >nul 2>nul && set PY=py
if not defined PY where python >nul 2>nul && set PY=python
if not defined PY (
  echo Python was not found. Run Install.bat first.
  pause
  exit /b 1
)

echo [1/3] Installing pythonnet (lets Python use the sensor library)...
%PY% -m pip install --user --upgrade pythonnet
if errorlevel 1 (
  echo pythonnet failed to install - see the messages above.
  pause
  exit /b 1
)

echo.
echo [2/3] Downloading LibreHardwareMonitor from its official GitHub releases...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop';" ^
  "$dest = Join-Path '%~dp0' 'LibreHardwareMonitor';" ^
  "$zip = Join-Path $env:TEMP 'LibreHardwareMonitor.zip';" ^
  "Invoke-WebRequest -UseBasicParsing 'https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases/latest/download/LibreHardwareMonitor.zip' -OutFile $zip;" ^
  "if (Test-Path $dest) { Remove-Item -Recurse -Force $dest };" ^
  "Expand-Archive $zip $dest -Force;" ^
  "Get-ChildItem -Recurse $dest | Unblock-File;" ^
  "Remove-Item $zip"
if errorlevel 1 (
  echo Download failed. Check your internet connection and run this again.
  pause
  exit /b 1
)

echo.
echo [3/3] One-time driver step.
echo LibreHardwareMonitor will open next. Approve the Windows prompt, and if it offers
echo to install the PawnIO driver, accept it. That signed driver is what lets Glance
echo read CPU and motherboard temperatures. Once you see temperatures, close it.
echo.
timeout /t 10
powershell -NoProfile -Command ^
  "$e = Get-ChildItem -Recurse -Filter LibreHardwareMonitor.exe (Join-Path '%~dp0' 'LibreHardwareMonitor') | Select-Object -First 1;" ^
  "if ($e) { Start-Process $e.FullName -Verb RunAs }"

echo.
echo All set. Restart Glance, open the Temp tab and click "Enable temperatures".
timeout /t 15
