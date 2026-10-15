@echo off
cd /d "%~dp0"
where pyw >nul 2>nul && (start "" pyw "%~dp0glance.pyw" & exit /b)
where pythonw >nul 2>nul && (start "" pythonw "%~dp0glance.pyw" & exit /b)
echo Python was not found. Run Install.bat first.
pause
