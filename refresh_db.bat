@echo off
cd /d "%~dp0"
title FRN database refresh

echo Starting public-data database refresh and validation.
echo The existing database is kept until validation succeeds.
echo.

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" refresh_db.py
) else (
    python refresh_db.py
)

if errorlevel 1 goto failed
echo.
echo Refresh completed. Check data\refresh_report_*.json.
pause
exit /b 0

:failed
echo.
echo Refresh failed. The existing database was kept.
pause
exit /b 1
