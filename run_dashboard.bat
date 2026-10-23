@echo off
cd /d "%~dp0"
set "UV_CACHE_DIR=%CD%\.uv-cache"
title FRN dashboard

if not exist ".venv\Scripts\python.exe" (
    echo Creating Python environment.
    "%USERPROFILE%\.local\bin\uv.exe" venv .venv
    if errorlevel 1 goto setup_failed
)
if not exist ".venv\Scripts\streamlit.exe" (
    echo Installing required packages.
    "%USERPROFILE%\.local\bin\uv.exe" pip install --python ".venv\Scripts\python.exe" -r requirements.txt setuptools
    if errorlevel 1 goto setup_failed
)
echo Refreshing public-data database.
.venv\Scripts\python.exe refresh_db.py
if errorlevel 1 goto refresh_failed
echo Starting Streamlit dashboard.
.venv\Scripts\streamlit.exe run app.py
pause
exit /b 0
:refresh_failed
echo Database refresh failed. Existing database was kept.
pause
exit /b 1
:setup_failed
echo Python environment or package installation failed.
pause
exit /b 1
