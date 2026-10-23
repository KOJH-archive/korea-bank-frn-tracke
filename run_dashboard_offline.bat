@echo off
cd /d "%~dp0"
set "UV_CACHE_DIR=%CD%\.uv-cache"
title FRN dashboard offline

if not exist ".venv\Scripts\python.exe" (
    "%USERPROFILE%\.local\bin\uv.exe" venv .venv
    if errorlevel 1 goto setup_failed
)
if not exist ".venv\Scripts\streamlit.exe" (
    "%USERPROFILE%\.local\bin\uv.exe" pip install --python ".venv\Scripts\python.exe" -r requirements.txt setuptools
    if errorlevel 1 goto setup_failed
)
echo Starting dashboard from existing database. No API refresh is run.
.venv\Scripts\streamlit.exe run app.py
pause
exit /b 0
:setup_failed
echo Python environment or package installation failed.
pause
exit /b 1
